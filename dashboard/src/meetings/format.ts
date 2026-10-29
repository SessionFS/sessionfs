/**
 * Pure helpers for the meeting surfaces (GitHub #116): error codes in plain
 * words, line-range URLs, dates, and client-side file reading. Kept free of
 * React so they are cheap to unit-test.
 */

import { ApiError, type MeetingDetail, type MeetingParticipant, type MeetingSummary } from '../api/client';
import { errorMessage } from '../utils/errorMessage';

/** Server caps (routes/meetings.py): a 1 MiB request body, then 512 KiB of
 *  normalised text and 10 000 lines (VTT/SRT timing is stripped first). */
export const MAX_BODY_BYTES = 1024 * 1024;
export const MAX_CONTENT_BYTES = 512 * 1024;
export const MAX_LINES = 10_000;
export const MAX_TITLE = 200;

/** Files the upload dialog reads client-side; VTT/SRT timing is stripped server-side. */
export const ACCEPTED_EXTENSIONS = ['.txt', '.md', '.vtt', '.srt'];

/** Where the AI key lives (judge settings); extraction uses it, else the org key. */
export const AI_KEY_SETTINGS_HREF = '/settings/data-protection';

// ── Error codes ──

/** The code of a meeting route error: `detail.error`, then `detail.code`. */
export function meetingErrorCode(err: unknown): string | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const details = err.details as { error?: unknown; code?: unknown } | undefined;
  if (details && typeof details.error === 'string') return details.error;
  if (details && typeof details.code === 'string') return details.code;
  // Numeric fallbacks ("404") are not codes.
  return err.code && !/^\d+$/.test(err.code) ? err.code : undefined;
}

/** `{pattern_name: count}` from a 422 dlp_blocked answer. */
export function dlpFindings(err: unknown): Record<string, number> {
  if (!(err instanceof ApiError)) return {};
  const findings = (err.details as { findings?: unknown } | undefined)?.findings;
  if (!findings || typeof findings !== 'object') return {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(findings as Record<string, unknown>)) {
    if (typeof v === 'number' && v > 0) out[k] = v;
  }
  return out;
}

/** "2 AWS access keys and 1 email address" — pattern names in words. */
export function describeFindings(findings: Record<string, number>): string {
  const parts = Object.entries(findings)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, n]) => {
      const words = name
        .replace(/[_-]+/g, ' ')
        .replace(/\baws\b/gi, 'AWS')
        .replace(/\bapi\b/gi, 'API')
        .replace(/\bssn\b/gi, 'SSN')
        .trim();
      return `${n} ${n === 1 ? words : pluralWords(words)}`;
    });
  if (parts.length === 0) return 'sensitive data';
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}

function pluralWords(words: string): string {
  if (/(ss|sh|ch|x)$/i.test(words)) return `${words}es`;
  if (/s$/i.test(words)) return words;
  return `${words}s`;
}

/** Plain-language message for an upload failure. */
export function uploadErrorMessage(err: unknown, captions = false): string {
  const code = meetingErrorCode(err);
  if (captions && (code === 'meeting_too_large' || (err instanceof ApiError && err.status === 413))) {
    // The client already keeps caption uploads under the body cap, so a 413
    // here comes from the limits applied after timestamps are removed.
    return 'After timestamps are removed, this transcript is still over 512 KB or 10,000 lines. Split it into parts and add each one.';
  }
  if (code === 'meeting_too_large' || (err instanceof ApiError && err.status === 413)) {
    return 'This transcript is too large. A meeting can hold up to 512 KB of text and 10,000 lines — split it into parts and add each one.';
  }
  if (code === 'dlp_blocked') {
    return `Your organization’s data protection policy blocked this transcript: it contains ${describeFindings(dlpFindings(err))}. Remove those and try again.`;
  }
  if (code === 'group_edit_forbidden' || (err instanceof ApiError && err.status === 403)) {
    return 'You can view meetings here but not add them.';
  }
  if (code === 'validation_error') return 'Check the title and date, then try again.';
  return errorMessage(err, 'The meeting could not be saved.');
}

/** Codes extraction itself produces (request errors and stored failure codes). */
const EXTRACTION_CODES = new Set([
  'llm_key_required',
  'helper_budget_exceeded',
  'extraction_busy',
  'timeout',
  'unreachable',
  'invalid_output',
  'output_truncated',
  'invalid_key',
  'access_denied',
  'model_not_found',
  'rate_limited',
  'insufficient_balance',
  'endpoint_blocked',
  'endpoint_not_api',
  'provider_error',
  'llm_request_failed',
]);

export function isExtractionCode(code: string | null | undefined): boolean {
  return !!code && EXTRACTION_CODES.has(code);
}

/** Why an extract request failed: extraction codes in words, anything else
 *  (edit rights, plan, a deleted meeting) through the general messages. */
export function extractErrorMessage(err: unknown): string {
  const code = meetingErrorCode(err);
  return isExtractionCode(code) ? extractionReason(code) : meetingActionError(err);
}

/** Why extraction stopped, in words. Covers request errors and stored failure codes. */
export function extractionReason(code: string | null | undefined): string {
  switch (code) {
    case 'llm_key_required':
      return 'No AI key is set up for you or your organization.';
    case 'helper_budget_exceeded':
      return 'Your organization’s monthly AI budget has been used up.';
    case 'extraction_busy':
      return 'Another extraction of yours is still running. Try again when it finishes.';
    case 'timeout':
      return 'The model took too long to answer.';
    case 'unreachable':
      return 'The model’s endpoint could not be reached.';
    case 'invalid_output':
      return 'The model’s answer could not be read as action items.';
    case 'output_truncated':
      return 'The model’s answer was cut off before it finished.';
    case 'invalid_key':
      return 'The AI key was rejected by the provider.';
    case 'access_denied':
      return 'The AI key doesn’t have access to that model.';
    case 'model_not_found':
      return 'The configured model doesn’t exist at the provider.';
    case 'rate_limited':
      return 'The provider is rate limiting requests. Try again in a minute.';
    case 'insufficient_balance':
      return 'The AI provider account is out of credit.';
    case 'endpoint_blocked':
      return 'The model endpoint is not allowed by this server.';
    case 'endpoint_not_api':
      return 'The model endpoint doesn’t look like an API.';
    case 'provider_error':
    case 'llm_request_failed':
      return 'The model provider returned an error.';
    default:
      return 'The model request failed.';
  }
}

/** Plain-language message for a failed write on one meeting (promote, dismiss…). */
export function meetingActionError(err: unknown): string {
  const code = meetingErrorCode(err);
  switch (code) {
    case 'item_not_pending':
      return 'This item was already handled — refresh to see its current state.';
    case 'item_not_dismissed':
      return 'This item is no longer dismissed.';
    case 'target_project_required':
      return 'Choose the project to create the ticket in.';
    case 'target_project_not_editable':
      return 'You can’t create tickets in that project.';
    case 'participant_not_eligible':
      return 'That person or persona can’t be matched in this meeting’s scope.';
    case 'assignee_not_eligible':
      return 'That person can’t be assigned tickets in this project.';
    case 'assignee_conflict':
      return 'A ticket can be assigned to a person or a persona, not both.';
    case 'jira_not_connected':
      return 'Your organization hasn’t connected Jira, so nothing was created. Turn off “Also publish to Jira”, or ask an org admin to connect it.';
    case 'jira_project_unmapped':
      return 'This project isn’t mapped to a Jira project, so nothing was created. Turn off “Also publish to Jira”, or ask an org admin to add a mapping.';
    case 'jira_edit_required':
      return 'Publishing to Jira needs edit rights on this project, so nothing was created. Turn off “Also publish to Jira” to create the ticket only.';
    case 'project_edit_forbidden':
    case 'group_edit_forbidden':
      return 'You can view this meeting but not change it.';
    default:
      if (err instanceof ApiError && err.status === 404) return 'This meeting no longer exists.';
      if (code && code.startsWith('jira')) return errorMessage(err);
      return errorMessage(err);
  }
}

// ── Upload warnings ──

export interface UploadWarnings {
  /** Id of an identical transcript already stored in this scope. */
  duplicateOf: string | null;
  /** Why `extract: true` didn't run (the meeting is stored either way). */
  extractionSkipped: string | null;
}

export function parseWarnings(warnings: readonly string[] | undefined): UploadWarnings {
  let duplicateOf: string | null = null;
  let extractionSkipped: string | null = null;
  for (const w of warnings ?? []) {
    const dup = /^duplicate_of:\s*(meet_[A-Za-z0-9]+)/.exec(w);
    if (dup) duplicateOf = dup[1];
    const skipped = /^extraction_skipped:\s*([a-z0-9_]+)/.exec(w);
    if (skipped) extractionSkipped = skipped[1];
  }
  return { duplicateOf, extractionSkipped };
}

// ── Line ranges (URL `lines=12-15`) ──

export interface LineRange {
  start: number;
  end: number;
}

export function parseLines(value: string | null | undefined): LineRange | null {
  if (!value) return null;
  const m = /^L?(\d{1,6})(?:-L?(\d{1,6}))?$/.exec(value.trim());
  if (!m) return null;
  const a = Number(m[1]);
  const b = m[2] !== undefined ? Number(m[2]) : a;
  if (a < 1 || b < 1) return null;
  return { start: Math.min(a, b), end: Math.max(a, b) };
}

export function formatLinesParam(range: LineRange): string {
  return range.start === range.end ? String(range.start) : `${range.start}-${range.end}`;
}

export function linesLabel(range: LineRange): string {
  return range.start === range.end ? `Line ${range.start}` : `Lines ${range.start}–${range.end}`;
}

// ── Links ──

/** In-app link to a meeting, wherever it lives. `/meetings/:id` redirects. */
export function meetingHref(meetingId: string, range?: LineRange | null): string {
  const base = `/meetings/${encodeURIComponent(meetingId)}`;
  return range ? `${base}?lines=${formatLinesParam(range)}` : base;
}

/** The page that shows a meeting: its project's Work → Meetings, or its group. */
export function meetingScopeHref(
  m: Pick<MeetingSummary, 'id' | 'project_id' | 'group_id' | 'org_id'>,
  lines?: string | null,
): string | null {
  const tail = `meeting=${encodeURIComponent(m.id)}${lines ? `&lines=${encodeURIComponent(lines)}` : ''}`;
  if (m.project_id) return `/projects/${encodeURIComponent(m.project_id)}?tab=work&sub=meetings&${tail}`;
  if (m.group_id) {
    const org = m.org_id ? `org=${encodeURIComponent(m.org_id)}&` : '';
    return `/settings/organization/groups/${encodeURIComponent(m.group_id)}?${org}${tail}`;
  }
  return null;
}

// ── Dates ──

const DATE_FMT = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });

/**
 * The meeting's date: `meeting_at` when given (a calendar date — the upload
 * dialog sends midnight UTC, so it is read in UTC), else the upload day.
 */
export function meetingDateLabel(m: Pick<MeetingSummary, 'meeting_at' | 'created_at'>): string {
  const iso = m.meeting_at ?? m.created_at;
  const d = new Date(m.meeting_at ? `${iso.slice(0, 10)}T00:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return '';
  return m.meeting_at ? DATE_FMT.format(d) : new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' }).format(d);
}

/** A due date (YYYY-MM-DD) as "Nov 4". */
export function dueLabel(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return date;
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(d);
}

/** The value the date input sends (`meeting_at`), or null when empty. */
export function meetingAtFromDate(date: string): string | null {
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? `${date}T00:00:00Z` : null;
}

// ── Files ──

export function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/** "Weekly sync 2026-10-28.vtt" → "Weekly sync 2026-10-28". */
export function titleFromFileName(name: string): string {
  const base = name.replace(/\.[^.]+$/, '').replace(/[_]+/g, ' ').replace(/\s+/g, ' ').trim();
  return base.slice(0, MAX_TITLE);
}

export function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function lineCount(text: string): number {
  if (!text) return 0;
  return text.replace(/\r\n?/g, '\n').replace(/\n+$/, '').split('\n').length;
}

/** "12 KB" / "1.2 MB". */
export function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Caption files lose their cue timing on the server before its text caps apply. */
export function isCaptionFile(name: string): boolean {
  return /\.(vtt|srt)$/i.test(name);
}

/** A picked file too big to even read (over the 1 MiB request cap). */
export function fileTooLarge(size: number): string | null {
  return size > MAX_BODY_BYTES
    ? 'This file is larger than 1 MB. Split the transcript into parts and add each one.'
    : null;
}

/**
 * Client-side size check, before anything is sent. Null when it fits.
 * Caption files (`captions`) are only held to the 1 MiB request cap: the
 * server strips their timing lines before the 512 KB / 10,000-line checks.
 */
export function contentTooLarge(text: string, captions = false): string | null {
  if (captions) {
    // The cap applies to the JSON request body: measure the content as it
    // will be encoded there (every newline becomes `\n`, quotes and
    // backslashes are escaped), leaving ~1 KB for the title and metadata.
    return byteLength(JSON.stringify(text)) > MAX_BODY_BYTES - 1024
      ? 'This file is over the 1 MB upload size limit. Split the transcript into parts and add each one.'
      : null;
  }
  if (byteLength(text) > MAX_CONTENT_BYTES) {
    return 'This transcript is larger than 512 KB. Split it into parts and add each one.';
  }
  if (lineCount(text) > MAX_LINES) {
    return 'This transcript has more than 10,000 lines. Split it into parts and add each one.';
  }
  return null;
}

/** Read a picked file as text (File.text where available, FileReader otherwise). */
export function readFileText(file: File): Promise<string> {
  if (typeof file.text === 'function') return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('Could not read the file'));
    reader.readAsText(file);
  });
}

// ── Transcript lines ──

/** Split a stored transcript into its numbered lines (1-based). */
export function transcriptLines(content: string): string[] {
  if (!content) return [];
  const lines = content.split('\n');
  // A trailing newline is not a line of its own.
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** `Speaker: text` → ["Speaker", "text"]; null when the line names no speaker. */
export function splitSpeaker(line: string): [string, string] | null {
  const m = /^([A-ZÀ-ɏ][\p{L}\p{M}0-9 .'’()-]{0,48}?):\s+(.*)$/u.exec(line);
  if (!m) return null;
  // "Note: ..." style prose and timestamps aren't speakers.
  if (/^\d/.test(m[1]) || m[1].split(' ').length > 4) return null;
  return [m[1], m[2]];
}

// ── Items + participants ──

export function itemCounts(m: Pick<MeetingDetail, 'action_items'>) {
  let pending = 0;
  let decisions = 0;
  for (const i of m.action_items) {
    if (i.kind === 'decision') decisions += 1;
    else if (i.status === 'pending') pending += 1;
  }
  return { pending, decisions };
}

export const RESOLUTION_LABEL: Record<string, string> = {
  exact_email: 'Matched by email',
  display_name: 'Matched by name',
  persona: 'Matched to a persona',
  manual: 'Matched by hand',
  unresolved: 'Not matched',
};

export function isResolved(p: Pick<MeetingParticipant, 'user_id' | 'persona_name'>): boolean {
  return !!(p.user_id || p.persona_name);
}
