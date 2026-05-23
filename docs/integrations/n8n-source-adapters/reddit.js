// Reddit JSON → Scout uniform signal shape (Phase 4c).
//
// Upstream: https://www.reddit.com/r/{subreddit}/new.json?limit=25
//   (descriptive User-Agent header required per Reddit API rules —
//    set on the upstream HTTP Request node, e.g.
//    "User-Agent: sessionfs-scout/1.0 by /u/sessionfs")
// Auth:     none for the public .json endpoints, but UA must be set.
// Cadence:  every 15-30 min is the floor; below that you'll get 429s.
//
// n8n Code node mode: "runOnceForEachItem" — input is one Reddit
// child wrapper, i.e. one `{ kind, data }` entry from `data.children`.
//
// PII rules (scout-signal-shape.md §5):
//   - Username in `author` field only, ALWAYS prefixed with `u/`.
//   - @-mentions in `selftext` body replaced with `[user]`.
//   - Subreddit names are public metadata — fine to include.

const wrapper = $json;
const post = (wrapper && wrapper.data) || wrapper;

if (!post || !post.id || !post.title) {
  return [];
}

// Filter — drop low-signal items outside our tracked subreddits.
const CORE_SUBREDDITS = new Set([
  'LocalLLaMA',
  'programming',
  'MachineLearning',
  'agi',
  'SessionFS',
  'ClaudeAI',
  'OpenAI',
  'singularity',
]);
const subreddit = String(post.subreddit || '').trim();
const score = Number(post.score) || 0;
if (score < 3 && !CORE_SUBREDDITS.has(subreddit)) {
  return [];
}

// URL — Reddit posts always have a `permalink`; sometimes also `url`
// (link-out). Prefer the permalink (it's the canonical comment thread).
const permalink = post.permalink ? `https://www.reddit.com${post.permalink}` : null;
if (!permalink || !permalink.startsWith('https://')) {
  return [];
}
const url = permalink;

// Title.
let title = String(post.title).trim();
if (title.length > 240) {
  title = title.slice(0, 239) + '…';
}

// Content — selftext is markdown. Scrub @-mentions BEFORE truncation
// so the truncate marker isn't injected mid-mention.
let content = '';
if (post.selftext && typeof post.selftext === 'string') {
  content = post.selftext
    // Reddit @-mentions: `u/username` or `/u/username` inside body.
    // Keep the mention-marker visible; redact the identity.
    .replace(/\/?u\/[A-Za-z0-9_-]+/g, '[user]')
    // Bare @-mentions some users still write.
    .replace(/@[A-Za-z0-9_-]+/g, '[user]')
    .trim();
}
if (content.length > 4000) {
  content = content.slice(0, 4000 - 14) + '\n\n[truncated]';
}

// posted_at — Reddit's `created_utc` is epoch seconds (float).
const createdUtc = Number(post.created_utc);
if (!createdUtc || isNaN(createdUtc)) {
  return [];
}
const posted_at = new Date(createdUtc * 1000).toISOString();

// Author — username goes in `author` ONLY, with `u/` prefix so the
// dashboard renderer can recognize-and-redact downstream. Drop
// `[deleted]` placeholders.
let author = '';
if (post.author && post.author !== '[deleted]') {
  author = `u/${String(post.author).slice(0, 78)}`;
}

// Signal strength: upvotes + 2*comments, normalized.
const numComments = Number(post.num_comments) || 0;
const signal_strength = Math.min(1.0, (score + 2 * numComments) / 200);

return [{
  json: {
    source: 'reddit',
    source_id: `reddit:${post.id}`,
    title,
    url,
    content,
    posted_at,
    author,
    signal_strength,
    raw: post,
  },
}];

// FIXTURE.
//
// INPUT (one `children[i]` from
// https://www.reddit.com/r/LocalLLaMA/new.json?limit=25):
//
//   {
//     "kind": "t3",
//     "data": {
//       "id": "abc1234",
//       "title": "Is anyone using SessionFS in production?",
//       "selftext": "Question for /u/jdoe — does the daemon stay up across reboots?",
//       "author": "curious_dev",
//       "subreddit": "LocalLLaMA",
//       "score": 24,
//       "num_comments": 8,
//       "permalink": "/r/LocalLLaMA/comments/abc1234/is_anyone_using_sessionfs_in_production/",
//       "created_utc": 1779791640.0,
//       "url": "https://www.reddit.com/r/LocalLLaMA/comments/abc1234/..."
//     }
//   }
//
// EXPECTED OUTPUT:
//
//   {
//     "source": "reddit",
//     "source_id": "reddit:abc1234",
//     "title": "Is anyone using SessionFS in production?",
//     "url": "https://www.reddit.com/r/LocalLLaMA/comments/abc1234/is_anyone_using_sessionfs_in_production/",
//     "content": "Question for [user] — does the daemon stay up across reboots?",
//     "posted_at": "2026-05-22T11:14:00.000Z",
//     "author": "u/curious_dev",
//     "signal_strength": 0.2,
//     "raw": { /* the `data` object above, untouched */ }
//   }
//
// (signal_strength = min(1.0, (24 + 2*8) / 200) = 0.20)
// (`/u/jdoe` in selftext → `[user]`; `u/curious_dev` stays in `author` field only.)
