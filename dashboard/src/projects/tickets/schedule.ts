import { ApiError, type Ticket } from '../../api/client';
import { errorMessage } from '../../utils/errorMessage';

/** The server's estimate grammar (routes/tickets.py `_ESTIMATE_RE`): one or
 *  more `<n><unit>` tokens, unit w|d|h|m, whitespace between them optional. */
export const ESTIMATE_RE = /^\s*(\d+[wdhm]\s*)+$/;
export const ESTIMATE_MAX = 32;

/** Statuses that never count as overdue (server `overdue` filter). */
export const TERMINAL_STATUSES = new Set(['done', 'closed', 'cancelled']);

/** Client-side check before sending an estimate; null when it is valid. */
export function estimateError(raw: string): string | null {
  const v = raw.trim();
  if (!v) return 'Enter an estimate, or use Clear to remove it.';
  if (v.length > ESTIMATE_MAX) return `Keep it to ${ESTIMATE_MAX} characters or fewer.`;
  if (!ESTIMATE_RE.test(v)) return 'Use Jira durations like 2d 4h, 30m or 1w 2d.';
  return null;
}

/** Today's calendar date in UTC, as "YYYY-MM-DD". The server's `overdue`
 *  filter compares against `date.today()`, and the API runs on Cloud Run in
 *  UTC, so the chip uses the same day boundary as the filter rather than the
 *  viewer's local date (which would disagree for a few hours a day). */
export function todayIso(now: Date = new Date()): string {
  const y = now.getUTCFullYear();
  const m = String(now.getUTCMonth() + 1).padStart(2, '0');
  const d = String(now.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** Past its due date and still open work. */
export function isOverdue(
  t: Pick<Ticket, 'status'> & { due_date?: string | null },
  today: string = todayIso(),
): boolean {
  return !!t.due_date && t.due_date < today && !TERMINAL_STATUSES.has(t.status);
}

/** Parse a "YYYY-MM-DD" calendar date as a local date (no UTC shift). */
function parseIsoDate(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

/** "Nov 15", or "Nov 15, 2027" outside the current year. */
export function formatDueDate(iso: string, now: Date = new Date()): string {
  const d = parseIsoDate(iso);
  if (!d) return iso;
  return d.toLocaleDateString('en', {
    month: 'short',
    day: 'numeric',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  });
}

/** "Oct 17, 2026" for a timestamp. */
export function formatDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' });
}

/** Field-level messages from a 422, keyed by the body field they name
 *  (`loc` ending in `due_date` / `estimated_effort`). */
export function scheduleFieldErrors(err: unknown): { due_date?: string; estimated_effort?: string } {
  const out: { due_date?: string; estimated_effort?: string } = {};
  if (!(err instanceof ApiError) || err.status !== 422) return out;
  const errors = err.details?.errors;
  if (!Array.isArray(errors)) return out;
  for (const e of errors) {
    if (!e || typeof e !== 'object') continue;
    const { loc, msg } = e as { loc?: unknown; msg?: unknown };
    if (!Array.isArray(loc) || typeof msg !== 'string') continue;
    const text = msg.replace(/^Value error,\s*/i, '');
    if (loc.includes('due_date')) out.due_date = out.due_date ? `${out.due_date} ${text}` : text;
    if (loc.includes('estimated_effort')) out.estimated_effort = out.estimated_effort ? `${out.estimated_effort} ${text}` : text;
  }
  return out;
}

/** A 422's field messages ("estimated_effort must be a Jira duration …")
 *  rather than the generic "Validation error"; anything else as usual. */
export function scheduleErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.status === 422) {
    const errors = err.details?.errors;
    if (Array.isArray(errors)) {
      const msgs = errors
        .map((e) => (e && typeof e === 'object' ? (e as { msg?: unknown }).msg : null))
        .filter((m): m is string => typeof m === 'string' && !!m)
        .map((m) => m.replace(/^Value error,\s*/i, ''));
      if (msgs.length) return msgs.join(' ');
    }
  }
  return errorMessage(err);
}
