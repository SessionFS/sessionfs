/**
 * Pure helpers for the session-helper drawer: tool-step labels, inline
 * citation markers, error classification and relative times. Kept free of
 * React so they are cheap to unit-test.
 */

import { ApiError, type HelperCitation } from '../api/client';
import { errorMessage } from '../utils/errorMessage';

/** aria-labelledby target for the drawer heading. */
export const TITLE_ID = 'helper-panel-title';

/** Server limit on a single question (`_MAX_MESSAGE_CHARS` in routes/helper.py). */
export const MAX_MESSAGE_CHARS = 20_000;

// ── Tool steps ──

export interface ToolStep {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: 'running' | 'ok' | 'error';
  /** Items the tool returned (sessions + KB entries + projects); null when unknown. */
  itemCount: number | null;
}

function str(args: Record<string, unknown>, key: string): string | null {
  const v = args[key];
  return typeof v === 'string' && v.trim() ? v.trim() : null;
}

function quote(text: string, max = 48): string {
  const clipped = text.length > max ? `${text.slice(0, max - 1)}…` : text;
  return `“${clipped}”`;
}

/** A human sentence for one tool step, in the running or finished tense. */
export function describeToolStep(step: Pick<ToolStep, 'name' | 'args' | 'status'>): string {
  const running = step.status === 'running';
  const verb = (doing: string, done: string) => (running ? doing : done);
  const a = step.args ?? {};
  const query = str(a, 'query');
  switch (step.name) {
    case 'search_sessions':
      return `${verb('Searching', 'Searched')} sessions${query ? ` for ${quote(query)}` : ''}`;
    case 'list_recent_sessions':
      return `${verb('Listing', 'Listed')} recent sessions`;
    case 'find_related_sessions': {
      const target = str(a, 'file_path') ?? str(a, 'error_text');
      return `${verb('Finding', 'Found')} related sessions${target ? ` for ${quote(target)}` : ''}`;
    }
    case 'get_session_summary':
      return `${verb('Reading', 'Read')} a session summary`;
    case 'get_session_excerpts':
      return `${verb('Reading', 'Read')} session excerpts${query ? ` about ${quote(query)}` : ''}`;
    case 'search_project_knowledge':
      return `${verb('Searching', 'Searched')} project knowledge${query ? ` for ${quote(query)}` : ''}`;
    case 'get_project_context':
      return `${verb('Reading', 'Read')} the project context`;
    case 'summarize_sessions': {
      const ids = Array.isArray(a.session_ids) ? a.session_ids.length : 0;
      return `${verb('Summarizing', 'Summarized')} ${ids > 0 ? `${ids} ${ids === 1 ? 'session' : 'sessions'}` : 'sessions'}`;
    }
    case 'sessions_touching_path': {
      const path = str(a, 'path');
      return `${verb('Finding', 'Found')} sessions that touched ${path ? quote(path) : 'a path'}`;
    }
    default:
      return `${verb('Running', 'Ran')} ${step.name.replace(/_/g, ' ')}`;
  }
}

export function countLabel(n: number, one = 'result', many = 'results'): string {
  if (n === 0) return `no ${many}`;
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

// ── Turns ──

export interface Turn {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  toolSteps: ToolStep[];
  citations: HelperCitation[];
  /** Assistant only: the question this answer is for (used by Retry). */
  question?: string;
  /** Assistant only. `stopped` = the user pressed Stop. */
  status?: 'streaming' | 'done' | 'stopped' | 'error';
  error?: HelperError;
}

// ── Citations ──

/** Same shape the server parses (`_CITATION_RE` in routes/helper.py). */
const CITATION_RE = /\[session:([A-Za-z0-9_-]+)(?:\s+msg:(\d+))?\]/g;

export interface Source {
  /** 1-based footnote number, by first appearance. */
  n: number;
  sessionId: string;
  title: string | null;
  messageIndexes: number[];
  /** False when the server dropped the citation (session not readable). */
  verified: boolean;
}

export const CITE_HREF_PREFIX = '#sfs-cite-';

/** In-answer href for footnote `n`, optionally pinned to one cited message. */
export function citeHref(n: number, messageIndex?: number): string {
  return `${CITE_HREF_PREFIX}${n}${messageIndex !== undefined ? `-m${messageIndex}` : ''}`;
}

/** Inverse of {@link citeHref}; null for any other href. */
export function parseCiteHref(href: string): { n: number; messageIndex?: number } | null {
  if (!href.startsWith(CITE_HREF_PREFIX)) return null;
  const m = /^(\d+)(?:-m(\d+))?$/.exec(href.slice(CITE_HREF_PREFIX.length));
  if (!m) return null;
  return { n: Number(m[1]), messageIndex: m[2] !== undefined ? Number(m[2]) : undefined };
}

/**
 * Dashboard link to a cited session, opened at one message when known
 * (`msg` is the 0-based messages.jsonl line number the server cites).
 */
export function sourceHref(sessionId: string, messageIndex?: number | null): string {
  const base = `/sessions/${encodeURIComponent(sessionId)}`;
  return messageIndex != null ? `${base}?msg=${messageIndex}` : base;
}

/** Drop a citation marker that is still being streamed in at the end of the text. */
function stripPartialMarker(text: string): string {
  const i = text.lastIndexOf('[');
  if (i < 0) return text;
  const tail = text.slice(i);
  if (tail.includes(']')) return text;
  if ('[session:'.startsWith(tail) || /^\[session:[A-Za-z0-9_-]*(?:\s+(?:m(?:s(?:g(?::\d*)?)?)?)?)?$/.test(tail)) {
    return text.slice(0, i);
  }
  return text;
}

/**
 * Turn `[session:<id> msg:<n>]` markers into numbered markdown links
 * (`[1](#sfs-cite-1-m<n>)`, or `#sfs-cite-1` without a message) and collect the sources they point at. Sessions are
 * numbered once, in order of first appearance; the server's citation events
 * supply titles and say which ids are readable. Sessions cited by the server
 * but never marked in the text are appended so the sources row is complete.
 *
 * While streaming, a marker cut off at the end of the text is hidden so the
 * reader never sees `[session:ses_ab` flash before it resolves.
 */
export function processCitations(
  text: string,
  citations: HelperCitation[],
  opts: { streaming?: boolean; settled?: boolean } = {},
): { markdown: string; sources: Source[] } {
  const byId = new Map<string, Source>();
  const known = new Map<string, HelperCitation>();
  for (const c of citations) if (!known.has(c.session_id)) known.set(c.session_id, c);

  const sourceFor = (sid: string): Source => {
    let s = byId.get(sid);
    if (!s) {
      const k = known.get(sid);
      s = {
        n: byId.size + 1,
        sessionId: sid,
        title: k?.title ?? null,
        messageIndexes: [],
        // Until the turn settles the server hasn't said yet; assume readable.
        verified: k !== undefined || !opts.settled,
      };
      byId.set(sid, s);
    }
    return s;
  };

  let body = text;
  if (opts.streaming) body = stripPartialMarker(body);

  const markdown = body.replace(CITATION_RE, (_m, sid: string, idx?: string) => {
    const s = sourceFor(sid);
    if (idx !== undefined) {
      const i = Number(idx);
      if (!s.messageIndexes.includes(i)) s.messageIndexes.push(i);
      return `[${s.n}](${citeHref(s.n, i)})`;
    }
    return `[${s.n}](${citeHref(s.n)})`;
  });

  for (const c of citations) {
    const s = sourceFor(c.session_id);
    if (c.message_index != null && !s.messageIndexes.includes(c.message_index)) {
      s.messageIndexes.push(c.message_index);
    }
  }
  for (const s of byId.values()) s.messageIndexes.sort((x, y) => x - y);
  return { markdown, sources: [...byId.values()] };
}

/** Plain-text copy of an answer: markers become [n], sources listed below. */
export function answerAsPlainText(text: string, citations: HelperCitation[]): string {
  const { sources } = processCitations(text, citations, { settled: true });
  const numbered = text.replace(CITATION_RE, (_m, sid: string) => {
    const s = sources.find((x) => x.sessionId === sid);
    return s ? `[${s.n}]` : '';
  });
  if (sources.length === 0) return numbered;
  const lines = sources.map((s) => `[${s.n}] ${s.title || s.sessionId} (session ${s.sessionId})`);
  return `${numbered}\n\nSources:\n${lines.join('\n')}`;
}

// ── Errors ──

export type HelperErrorKind =
  | 'busy'
  | 'key_required'
  | 'scope_mismatch'
  | 'upgrade_required'
  | 'network'
  | 'disconnected'
  | 'provider'
  | 'other';

export interface HelperError {
  kind: HelperErrorKind;
  message: string;
}

/** The `code` from either envelope shape (`{error:{code}}` or `{detail:{code}}`). */
function errorCode(err: ApiError): string | undefined {
  if (err.code) return err.code;
  try {
    const json = JSON.parse(err.raw) as { detail?: { code?: unknown }; error?: { code?: unknown } };
    const code = json?.detail?.code ?? json?.error?.code;
    return typeof code === 'string' ? code : undefined;
  } catch {
    return undefined;
  }
}

export function classifyError(err: unknown): HelperError {
  if (err instanceof ApiError) {
    const code = errorCode(err);
    const message = errorMessage(err);
    if (code === 'helper_busy') return { kind: 'busy', message };
    if (code === 'helper_key_required') return { kind: 'key_required', message };
    if (code === 'scope_mismatch') return { kind: 'scope_mismatch', message };
    if (code === 'upgrade_required' || err.status === 403) return { kind: 'upgrade_required', message };
    if (code === 'provider_error' || err.status === 502) return { kind: 'provider', message };
    return { kind: 'other', message };
  }
  if (err instanceof TypeError) {
    return { kind: 'network', message: "Couldn't reach SessionFS. Check your connection and try again." };
  }
  return { kind: 'other', message: errorMessage(err, 'The helper failed to answer.') };
}

/** Error from an SSE `error` frame. */
export function streamError(code: string | undefined, message: string | undefined): HelperError {
  const msg = message || 'The helper failed to answer.';
  if (code === 'helper_key_required') return { kind: 'key_required', message: msg };
  if (code === 'provider_error') return { kind: 'provider', message: msg };
  return { kind: 'other', message: msg };
}

export const ERROR_TITLES: Record<HelperErrorKind, string> = {
  busy: 'The helper is busy',
  key_required: 'No model key available',
  scope_mismatch: 'This conversation has a different scope',
  upgrade_required: 'Not available on your plan',
  network: 'Connection problem',
  disconnected: 'The answer was cut off',
  provider: 'The model returned an error',
  other: 'Something went wrong',
};

// ── Time ──

export function relativeTime(iso: string, now = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  if (d === 1) return 'yesterday';
  if (d < 7) return `${d} days ago`;
  return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export type HistoryGroup = 'Today' | 'Yesterday' | 'Previous 7 days' | 'Older';

export function historyGroup(iso: string, now = Date.now()): HistoryGroup {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 'Older';
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  const today = start.getTime();
  if (t >= today) return 'Today';
  if (t >= today - 86_400_000) return 'Yesterday';
  if (t >= today - 7 * 86_400_000) return 'Previous 7 days';
  return 'Older';
}
