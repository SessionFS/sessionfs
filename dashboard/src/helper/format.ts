/**
 * Pure helpers for the session-helper drawer: tool-step labels, inline
 * citation markers, error classification and relative times. Kept free of
 * React so they are cheap to unit-test.
 */

import {
  ApiError,
  type HelperCitation,
  type HelperCitationRef,
  type HelperDocCitation,
  type HelperTicketCitation,
  type HelperWikiCitation,
} from '../api/client';
import { errorMessage } from '../utils/errorMessage';
import type { TurnAction } from './actions';

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

/** A string-or-list model/provider filter argument as display text. */
function filterValues(args: Record<string, unknown>, key: string): string | null {
  const v = args[key];
  const list = Array.isArray(v) ? v : typeof v === 'string' ? [v] : [];
  const values = list.filter((x): x is string => typeof x === 'string' && x.trim() !== '').map((x) => x.trim());
  return values.length ? values.join(' or ') : null;
}

/** ` using “deepseek”` when a step filters by model or provider. */
function modelSuffix(args: Record<string, unknown>): string {
  const target = filterValues(args, 'model') ?? filterValues(args, 'provider');
  return target ? ` using ${quote(target)}` : '';
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
      return `${verb('Searching', 'Searched')} sessions${query ? ` for ${quote(query)}` : ''}${modelSuffix(a)}`;
    case 'list_recent_sessions':
      return `${verb('Listing', 'Listed')} ${modelSuffix(a) ? `sessions${modelSuffix(a)}` : 'recent sessions'}`;
    case 'list_session_models':
      return `${verb('Checking', 'Checked')} which models your sessions used`;
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
    case 'search_sessionfs_docs':
      return `${verb('Searching', 'Searched')} the SessionFS docs${query ? ` for ${quote(query)}` : ''}`;
    case 'get_sessionfs_doc': {
      const slug = str(a, 'slug');
      return `${verb('Reading', 'Read')} the SessionFS docs${slug ? ` (${quote(slug)})` : ''}`;
    }
    case 'summarize_sessions': {
      const ids = Array.isArray(a.session_ids) ? a.session_ids.length : 0;
      return `${verb('Summarizing', 'Summarized')} ${ids > 0 ? `${ids} ${ids === 1 ? 'session' : 'sessions'}` : 'sessions'}`;
    }
    case 'sessions_touching_path': {
      const path = str(a, 'path');
      return `${verb('Finding', 'Found')} sessions that touched ${path ? quote(path) : 'a path'}`;
    }
    case 'set_project_auto_narrative':
      return `${verb('Preparing', 'Prepared')} an Auto Narrative change`;
    case 'trash_sessions':
      return `${verb('Preparing', 'Prepared')} a move to Trash${modelSuffix(a)}`;
    case 'propose_ticket':
      return `${verb('Preparing', 'Prepared')} to create a ticket`;
    case 'propose_assignment':
      return `${verb('Preparing', 'Prepared')} to assign a ticket`;
    case 'propose_comment':
      return `${verb('Preparing', 'Prepared')} to comment on a ticket`;
    default:
      return `${verb('Running', 'Ran')} ${step.name.replace(/_/g, ' ')}`;
  }
}

/**
 * Append a streamed text delta to the answer so far. When the delta is the
 * first text of a new model step (after a tool round), start a new paragraph —
 * otherwise the steps run together ("…ran on DeepSeek.I can see…"). Mirrors
 * `_step_separator` in routes/helper.py; the server already streams the break,
 * so this only adds what is still missing.
 */
export function appendStepText(previous: string, delta: string, newStep: boolean): string {
  if (!newStep || !previous.trim()) return previous + delta;
  if (previous.endsWith('\n\n') || delta.startsWith('\n\n')) return previous + delta;
  if (previous.endsWith('\n') && delta.startsWith('\n')) return previous + delta;
  if (previous.endsWith('\n') || delta.startsWith('\n')) return `${previous}\n${delta}`;
  return `${previous}\n\n${delta}`;
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
  citations: HelperCitationRef[];
  /** Assistant only: the question this answer is for (used by Retry). */
  question?: string;
  /** Assistant only. `stopped` = the user pressed Stop. */
  status?: 'streaming' | 'done' | 'stopped' | 'error';
  error?: HelperError;
  /** Assistant only: write actions proposed in this answer, awaiting confirm. */
  actions?: TurnAction[];
  /** Assistant only: tokens this answer used (the `done` event / stored row). */
  usage?: TokenUsage;
}

// ── Token usage ──

export interface TokenUsage {
  input: number;
  output: number;
}

/** 950 → "950", 1_234 → "1.2k", 12_345 → "12k", 2_400_000 → "2.4M". */
export function compactTokens(n: number): string {
  const v = Math.max(0, Math.round(n));
  const fmt = (x: number, unit: string) => {
    const s = x >= 10 ? String(Math.round(x)) : x.toFixed(1).replace(/\.0$/, '');
    return `${s}${unit}`;
  };
  if (v < 1000) return String(v);
  if (v < 999_500) return fmt(v / 1000, 'k');
  if (v < 999_500_000) return fmt(v / 1_000_000, 'M');
  return fmt(v / 1_000_000_000, 'B');
}

/** Token counts from a `done` event or a stored message (either field shape). */
export function usageFrom(src: {
  input_tokens?: unknown;
  output_tokens?: unknown;
  usage?: { input_tokens?: unknown; output_tokens?: unknown } | null;
}): TokenUsage | undefined {
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0);
  const input = num(src.input_tokens ?? src.usage?.input_tokens);
  const output = num(src.output_tokens ?? src.usage?.output_tokens);
  return input > 0 || output > 0 ? { input, output } : undefined;
}

// ── Citations ──

/**
 * All four marker kinds in one pass so footnote numbers stay shared and
 * ordered. Same shapes the server parses (`_CITATION_RE` / `_DOC_CITATION_RE`
 * / `_TICKET_CITATION_RE` / `_WIKI_CITATION_RE` in routes/helper.py). Doc
 * slugs and anchors use the docs index's own charset, so an unclosed `[doc:`
 * can't swallow the prose or citations after it.
 */
const MARKER_RE =
  /\[session:([A-Za-z0-9_-]+)(?:\s+msg:(\d+))?\]|\[doc:([a-z0-9][a-z0-9/_-]*)(?:#([a-z0-9_-]+))?\]|\[ticket:(tk_[0-9a-f]{16})\]|\[wiki:(proj_[0-9a-f]{16})\/([^\]\s]+)\]/g;

export interface SessionSource {
  kind: 'session';
  /** 1-based footnote number, by first appearance. */
  n: number;
  sessionId: string;
  title: string | null;
  messageIndexes: number[];
  /** False when the server dropped the citation (session not readable). */
  verified: boolean;
  /** ISO timestamp the session was last updated; null for old messages. */
  at: string | null;
}

export interface DocSource {
  kind: 'doc';
  /** 1-based footnote number, by first appearance (shared with sessions). */
  n: number;
  slug: string;
  anchor: string | null;
  title: string;
  /**
   * The docs URL, only ever from the server's `doc_citation` event (never
   * built here), so an unverified marker has no link. Null until verified.
   */
  url: string | null;
  /**
   * False once the turn settled without a matching `doc_citation` event (the
   * server found no such page/section). Like sessions, true while streaming
   * — but with `url` still null the chip stays inert until verified.
   */
  verified: boolean;
}

export interface TicketSource {
  kind: 'ticket';
  /** 1-based footnote number, by first appearance. */
  n: number;
  ticketId: string;
  projectId: string | null;
  title: string | null;
  /** False when the server dropped the citation (not from a tool this turn). */
  verified: boolean;
  at: string | null;
}

export interface WikiSource {
  kind: 'wiki';
  /** 1-based footnote number, by first appearance. */
  n: number;
  projectId: string;
  slug: string;
  title: string | null;
  /** False when the server dropped the citation (not from a tool this turn). */
  verified: boolean;
  at: string | null;
}

export type Source = SessionSource | DocSource | TicketSource | WikiSource;

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

/** Dashboard link to a cited sessionfs ticket (Work → Tickets, expanded). */
export function ticketHref(projectId: string, ticketId: string): string {
  return `/projects/${encodeURIComponent(projectId)}?tab=work&sub=tickets&ticket=${encodeURIComponent(ticketId)}`;
}

/** Dashboard link to a cited wiki page (Knowledge → Pages, that page open). */
export function wikiHref(projectId: string, slug?: string | null): string {
  const base = `/projects/${encodeURIComponent(projectId)}?tab=knowledge&sub=pages`;
  return slug ? `${base}&page=${encodeURIComponent(slug)}` : base;
}

/** Drop a citation marker that is still being streamed in at the end of the text. */
function stripPartialMarker(text: string): string {
  const i = text.lastIndexOf('[');
  if (i < 0) return text;
  const tail = text.slice(i);
  if (tail.includes(']')) return text;
  if (
    '[session:'.startsWith(tail) ||
    /^\[session:[A-Za-z0-9_-]*(?:\s+(?:m(?:s(?:g(?::\d*)?)?)?)?)?$/.test(tail) ||
    '[doc:'.startsWith(tail) ||
    /^\[doc:(?:[a-z0-9][a-z0-9/_-]*(?:#[a-z0-9_-]*)?)?$/.test(tail) ||
    '[ticket:'.startsWith(tail) ||
    /^\[ticket:(?:tk_[0-9a-f]{0,16})?$/.test(tail) ||
    '[wiki:'.startsWith(tail) ||
    /^\[wiki:(?:proj_[0-9a-f]{0,16}\/[^\]\s]*)?$/.test(tail)
  ) {
    return text.slice(0, i);
  }
  return text;
}

/**
 * Turn `[session:<id> msg:<n>]` and `[doc:<slug>#<anchor>]` markers into
 * numbered markdown links (`[1](#sfs-cite-1-m<n>)`, or `#sfs-cite-1` without
 * a message/anchor) and collect the sources they point at. Sources are
 * numbered once, in order of first appearance across both kinds; the server's
 * citation events supply titles, and for sessions say which ids are readable.
 * Sessions cited by the server but never marked in the text are appended so
 * the sources row is complete.
 *
 * While streaming, a marker cut off at the end of the text is hidden so the
 * reader never sees `[session:ses_ab` or `[doc:integ` flash before it resolves.
 */
export function processCitations(
  text: string,
  citations: HelperCitationRef[],
  opts: { streaming?: boolean; settled?: boolean } = {},
): { markdown: string; sources: Source[] } {
  const sessionCitations: HelperCitation[] = [];
  const docCitations: HelperDocCitation[] = [];
  const ticketCitations: HelperTicketCitation[] = [];
  const wikiCitations: HelperWikiCitation[] = [];
  for (const c of citations) {
    if ('session_id' in c) sessionCitations.push(c);
    else if ('ticket_id' in c) ticketCitations.push(c);
    else if ('project_id' in c && 'slug' in c) wikiCitations.push(c);
    else docCitations.push(c);
  }

  const knownSessions = new Map<string, HelperCitation>();
  for (const c of sessionCitations) if (!knownSessions.has(c.session_id)) knownSessions.set(c.session_id, c);
  const knownDocs = new Map<string, HelperDocCitation>();
  for (const c of docCitations) {
    const key = `${c.slug}#${c.anchor ?? ''}`;
    if (!knownDocs.has(key)) knownDocs.set(key, c);
  }
  const knownTickets = new Map<string, HelperTicketCitation>();
  for (const c of ticketCitations) if (!knownTickets.has(c.ticket_id)) knownTickets.set(c.ticket_id, c);
  const knownWikis = new Map<string, HelperWikiCitation>();
  for (const c of wikiCitations) {
    const key = `${c.project_id}/${c.slug}`;
    if (!knownWikis.has(key)) knownWikis.set(key, c);
  }

  const sources: Source[] = [];

  const sessionSourceFor = (sid: string): SessionSource => {
    let s = sources.find((x): x is SessionSource => x.kind === 'session' && x.sessionId === sid);
    if (!s) {
      const k = knownSessions.get(sid);
      s = {
        kind: 'session',
        n: sources.length + 1,
        sessionId: sid,
        title: k?.title ?? null,
        messageIndexes: [],
        // Until the turn settles the server hasn't said yet; assume readable.
        verified: k !== undefined || !opts.settled,
        at: k?.at ?? null,
      };
      sources.push(s);
    }
    return s;
  };

  const docSourceFor = (slug: string, anchor: string | null): DocSource => {
    let s = sources.find((x): x is DocSource => x.kind === 'doc' && x.slug === slug && x.anchor === anchor);
    if (!s) {
      const k = knownDocs.get(`${slug}#${anchor ?? ''}`);
      s = {
        kind: 'doc',
        n: sources.length + 1,
        slug,
        anchor,
        title: k?.title ?? slug,
        url: k?.url ?? null,
        verified: k !== undefined || !opts.settled,
      };
      sources.push(s);
    }
    return s;
  };

  const ticketSourceFor = (ticketId: string): TicketSource => {
    let s = sources.find((x): x is TicketSource => x.kind === 'ticket' && x.ticketId === ticketId);
    if (!s) {
      const k = knownTickets.get(ticketId);
      s = {
        kind: 'ticket',
        n: sources.length + 1,
        ticketId,
        projectId: k?.project_id ?? null,
        title: k?.title ?? null,
        verified: k !== undefined || !opts.settled,
        at: k?.at ?? null,
      };
      sources.push(s);
    }
    return s;
  };

  const wikiSourceFor = (projectId: string, slug: string): WikiSource => {
    let s = sources.find((x): x is WikiSource => x.kind === 'wiki' && x.projectId === projectId && x.slug === slug);
    if (!s) {
      const k = knownWikis.get(`${projectId}/${slug}`);
      s = {
        kind: 'wiki',
        n: sources.length + 1,
        projectId,
        slug,
        title: k?.title ?? slug,
        verified: k !== undefined || !opts.settled,
        at: k?.at ?? null,
      };
      sources.push(s);
    }
    return s;
  };

  let body = text;
  if (opts.streaming) body = stripPartialMarker(body);

  const markdown = body.replace(
    MARKER_RE,
    (
      _m,
      sid?: string,
      idx?: string,
      docSlug?: string,
      docAnchor?: string,
      ticketId?: string,
      wikiProject?: string,
      wikiSlug?: string,
    ) => {
      if (sid !== undefined) {
        const s = sessionSourceFor(sid);
        if (idx !== undefined) {
          const i = Number(idx);
          if (!s.messageIndexes.includes(i)) s.messageIndexes.push(i);
          return `[${s.n}](${citeHref(s.n, i)})`;
        }
        return `[${s.n}](${citeHref(s.n)})`;
      }
      if (ticketId !== undefined) {
        const s = ticketSourceFor(ticketId);
        return `[${s.n}](${citeHref(s.n)})`;
      }
      if (wikiProject !== undefined) {
        const s = wikiSourceFor(wikiProject, wikiSlug ?? '');
        return `[${s.n}](${citeHref(s.n)})`;
      }
      const s = docSourceFor(docSlug!, docAnchor ?? null);
      return `[${s.n}](${citeHref(s.n)})`;
    },
  );

  for (const c of sessionCitations) {
    const s = sessionSourceFor(c.session_id);
    if (c.message_index != null && !s.messageIndexes.includes(c.message_index)) {
      s.messageIndexes.push(c.message_index);
    }
  }
  for (const c of docCitations) docSourceFor(c.slug, c.anchor);
  for (const c of ticketCitations) ticketSourceFor(c.ticket_id);
  for (const c of wikiCitations) wikiSourceFor(c.project_id, c.slug);
  for (const s of sources) if (s.kind === 'session') s.messageIndexes.sort((x, y) => x - y);
  return { markdown, sources };
}

/** Plain-text copy of an answer: markers become [n], sources listed below. */
export function answerAsPlainText(text: string, citations: HelperCitationRef[]): string {
  const { sources } = processCitations(text, citations, { settled: true });
  const numbered = text.replace(
    MARKER_RE,
    (
      _m,
      sid?: string,
      _idx?: string,
      docSlug?: string,
      docAnchor?: string,
      ticketId?: string,
      wikiProject?: string,
      wikiSlug?: string,
    ) => {
      if (sid !== undefined) {
        const s = sources.find((x): x is SessionSource => x.kind === 'session' && x.sessionId === sid);
        return s ? `[${s.n}]` : '';
      }
      if (ticketId !== undefined) {
        const s = sources.find((x): x is TicketSource => x.kind === 'ticket' && x.ticketId === ticketId);
        return s?.verified ? `[${s.n}]` : '';
      }
      if (wikiProject !== undefined) {
        const s = sources.find(
          (x): x is WikiSource => x.kind === 'wiki' && x.projectId === wikiProject && x.slug === (wikiSlug ?? ''),
        );
        return s?.verified ? `[${s.n}]` : '';
      }
      const s = sources.find(
        (x): x is DocSource => x.kind === 'doc' && x.slug === docSlug! && x.anchor === (docAnchor ?? null),
      );
      return s?.url ? `[${s.n}]` : '';
    },
  );
  // A marker the server didn't confirm (doc without a URL, or an unverified
  // ticket/wiki) has no link: drop it from the copy.
  const listed = sources.filter((s) => s.kind === 'session' || (s.kind === 'doc' ? !!s.url : s.verified));
  if (listed.length === 0) return numbered;
  const lines = listed.map((s) => {
    if (s.kind === 'doc') return `[${s.n}] ${s.title} (${s.url})`;
    if (s.kind === 'ticket') return `[${s.n}] ${s.title || s.ticketId} (ticket ${s.ticketId})`;
    if (s.kind === 'wiki') return `[${s.n}] ${s.title || s.slug} (wiki ${s.projectId}/${s.slug})`;
    return `[${s.n}] ${s.title || s.sessionId} (session ${s.sessionId})`;
  });
  return `${numbered}\n\nSources:\n${lines.join('\n')}`;
}

/**
 * Compact relative age for a cited source, from the ``at`` timestamp the
 * server sends with the citation. Returns null (no badge) when ``at`` is
 * missing or unparseable — older stored citations have none, and the chip
 * must never claim an age the server didn't provide.
 */
export function citationAgeLabel(at: string | null | undefined, now = Date.now()): string | null {
  if (!at) return null;
  const t = Date.parse(at);
  if (Number.isNaN(t)) return null;
  const days = Math.floor(Math.max(0, now - t) / 86_400_000);
  if (days <= 0) return 'today';
  if (days < 30) return `${days}d`;
  if (days < 365) return `${Math.floor(days / 30)}mo`;
  return `${Math.floor(days / 365)}y`;
}

/**
 * The age as words for a screen reader ("updated 15 days ago"), or null
 * when there is no age badge.
 */
export function citationAgeSpoken(at: string | null | undefined, now = Date.now()): string | null {
  const label = citationAgeLabel(at, now);
  if (label === null) return null;
  if (label === 'today') return 'updated today';
  const n = Number.parseInt(label, 10);
  const unit = label.endsWith('mo') ? 'month' : label.endsWith('y') ? 'year' : 'day';
  return `updated ${n} ${unit}${n === 1 ? '' : 's'} ago`;
}

/** Exact timestamp for a cited source's tooltip; empty when unknown. */
export function citationTimestamp(at: string | null | undefined): string {
  if (!at) return '';
  const t = Date.parse(at);
  if (Number.isNaN(t)) return '';
  return new Date(t).toLocaleString();
}

// ── Errors ──

export type HelperErrorKind =
  | 'busy'
  | 'key_required'
  | 'scope_mismatch'
  | 'upgrade_required'
  | 'budget'
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
    if (code === 'helper_budget_exceeded') return { kind: 'budget', message };
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
  if (code === 'helper_budget_exceeded') return { kind: 'budget', message: msg };
  return { kind: 'other', message: msg };
}

export const ERROR_TITLES: Record<HelperErrorKind, string> = {
  busy: 'The helper is busy',
  key_required: 'No model key available',
  scope_mismatch: 'This conversation has a different scope',
  upgrade_required: 'Not available on your plan',
  budget: 'Monthly budget reached',
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

// ── References: tickets, projects, sessions, PRs, commits ──

export type RefKind = 'ticket' | 'project' | 'session';

/** In-answer href for an id reference; the answer's link renderer maps it to
 *  a preview button (tickets) or an in-app link (projects, sessions). A `#`
 *  href keeps it inside Markdown's safe-link filter. */
export const REF_HREF_PREFIX = '#sfs-ref-';

// Same alphabet and length as the server's session-id check (case kept loose
// for capture-tool ids such as Amp's).
const TICKET_ID = /^tk_[0-9a-f]{16}$/;
const PROJECT_ID = /^proj_[0-9a-f]{16}$/;
const SESSION_ID = /^ses_[A-Za-z0-9]{8,40}$/;

/** The id pattern for a kind. A switch, not an object lookup: the kind comes
 *  from model-written text, and keys like `constructor` must not resolve. */
function refIdPattern(kind: string): RegExp | null {
  switch (kind) {
    case 'ticket':
      return TICKET_ID;
    case 'project':
      return PROJECT_ID;
    case 'session':
      return SESSION_ID;
    default:
      return null;
  }
}

export function refHref(kind: RefKind, id: string): string {
  return `${REF_HREF_PREFIX}${kind}-${id}`;
}

/** Inverse of {@link refHref}; null for any other href or a malformed id. */
export function parseRefHref(href: string): { kind: RefKind; id: string } | null {
  if (!href.startsWith(REF_HREF_PREFIX)) return null;
  const rest = href.slice(REF_HREF_PREFIX.length);
  const dash = rest.indexOf('-');
  if (dash < 0) return null;
  const kind = rest.slice(0, dash);
  const id = rest.slice(dash + 1);
  const pattern = refIdPattern(kind);
  if (!pattern || !pattern.test(id)) return null;
  return { kind: kind as RefKind, id };
}

/** Where PR and commit references can point: only a project with exactly one
 *  repo that GitHub verified (the stored remote has no host, so another
 *  provider can't be told apart from GitHub). */
export interface RepoLinks {
  provider: 'github';
  /** `owner/repo`. */
  path: string;
}

export function repoLinksFor(
  repos: ReadonlyArray<{ git_remote_normalized: string; provider: string | null }> | undefined,
): RepoLinks | null {
  if (!repos || repos.length !== 1) return null;
  const [repo] = repos;
  if (repo.provider !== 'github') return null;
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo.git_remote_normalized)) return null;
  return { provider: 'github', path: repo.git_remote_normalized };
}

const ID_BODY = String.raw`tk_[0-9a-f]{16}|proj_[0-9a-f]{16}|ses_[A-Za-z0-9]{8,40}`;
// An id stands alone: not glued to a word, a path segment or a URL piece.
const BEFORE = String.raw`(?<![A-Za-z0-9_/.@#-])`;
const AFTER = String.raw`(?![A-Za-z0-9_-])`;
const ID_RE = new RegExp(`${BEFORE}(${ID_BODY})${AFTER}`, 'g');
const PR_RE = /(?<![A-Za-z0-9_])(PR|[Pp]ull request)\s?#(\d{1,7})(?![\d])/g;
// 7–40 hex chars with at least one digit and one letter (so plain numbers
// and words like "deadbeef" or "facade" stay text).
const SHA_BODY = '(?=[0-9a-f]*\\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}';
const SHA_RE = new RegExp(`${BEFORE}(${SHA_BODY})${AFTER}`, 'g');
const SHA_ONLY = new RegExp(`^${SHA_BODY}$`);

function idKind(id: string): RefKind {
  if (id.startsWith('tk_')) return 'ticket';
  if (id.startsWith('proj_')) return 'project';
  return 'session';
}

/** A run of plain text split into text and reference links. */
export type RefSegment = string | { text: string; url: string };

/**
 * Split plain text (one markdown text node) at the references it mentions.
 * `holdTail` leaves a reference that ends the text alone — while an answer
 * streams, `839c3a7` may still grow into a longer SHA.
 */
export function splitRefs(text: string, repo: RepoLinks | null, holdTail = false): RefSegment[] {
  type Hit = { start: number; end: number; text: string; url: string };
  const hits: Hit[] = [];
  for (const m of text.matchAll(ID_RE)) {
    hits.push({ start: m.index, end: m.index + m[0].length, text: m[1], url: refHref(idKind(m[1]), m[1]) });
  }
  if (repo) {
    const base = `https://github.com/${repo.path}`;
    for (const m of text.matchAll(PR_RE)) {
      hits.push({ start: m.index, end: m.index + m[0].length, text: m[0], url: `${base}/pull/${m[2]}` });
    }
    for (const m of text.matchAll(SHA_RE)) {
      hits.push({ start: m.index, end: m.index + m[0].length, text: m[1], url: `${base}/commit/${m[1]}` });
    }
  }
  hits.sort((a, b) => a.start - b.start);
  const out: RefSegment[] = [];
  let pos = 0;
  for (const h of hits) {
    if (h.start < pos) continue; // overlapping match: the first one wins
    if (holdTail && h.end === text.length) continue;
    if (h.start > pos) out.push(text.slice(pos, h.start));
    out.push({ text: h.text, url: h.url });
    pos = h.end;
  }
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

/** Link target for an inline code span holding exactly one reference. */
export function codeRefTarget(code: string, repo: RepoLinks | null): string | null {
  const t = code.trim();
  for (const kind of ['ticket', 'project', 'session'] as const) {
    if (refIdPattern(kind)!.test(t)) return refHref(kind, t);
  }
  if (repo && SHA_ONLY.test(t)) return `https://github.com/${repo.path}/commit/${t}`;
  return null;
}

// Minimal mdast shapes, so the dashboard doesn't depend on @types/mdast.
interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}

// Never linkified inside: existing links (inline, reference, autolink
// literals), definitions, images, code blocks and raw HTML.
const SKIP = new Set(['link', 'linkReference', 'definition', 'image', 'imageReference', 'code', 'html']);

/**
 * Remark plugin that makes the references an answer mentions clickable:
 * tickets (`tk_…`) open a preview, projects (`proj_…`) and sessions (`ses_…`)
 * link into the dashboard, and — when the scope's project has one GitHub repo
 * — `PR #123` and commit SHAs link to GitHub. It works on the parsed tree, so
 * code blocks (also inside quotes and lists), existing links of every form and
 * images are left alone by construction. An inline code span is linked only
 * when it holds exactly one reference.
 *
 * `streaming` holds back a reference that ends the answer so far.
 */
export function remarkRefs(opts: { repo?: RepoLinks | null; streaming?: boolean } = {}) {
  const repo = opts.repo ?? null;
  return () => (tree: MdNode) => {
    // The last text node in document order: the one a stream is still writing.
    let last: MdNode | null = null;
    if (opts.streaming) {
      const find = (n: MdNode) => {
        if (SKIP.has(n.type)) return;
        if (n.type === 'text') last = n;
        n.children?.forEach(find);
      };
      find(tree);
    }
    const walk = (node: MdNode) => {
      if (!node.children) return;
      const next: MdNode[] = [];
      for (const child of node.children) {
        if (child.type === 'text' && child.value) {
          const parts = splitRefs(child.value, repo, child === last);
          if (parts.length === 1 && typeof parts[0] === 'string') {
            next.push(child);
          } else {
            for (const p of parts) {
              next.push(
                typeof p === 'string'
                  ? { type: 'text', value: p }
                  : { type: 'link', url: p.url, children: [{ type: 'text', value: p.text }] },
              );
            }
          }
        } else if (child.type === 'inlineCode') {
          const url = codeRefTarget(child.value ?? '', repo);
          next.push(url ? { type: 'link', url, children: [child] } : child);
        } else {
          if (!SKIP.has(child.type)) walk(child);
          next.push(child);
        }
      }
      node.children = next;
    };
    walk(tree);
  };
}
