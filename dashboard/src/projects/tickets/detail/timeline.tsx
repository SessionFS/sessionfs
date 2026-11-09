import type { ReactNode } from 'react';
import type { JiraSyncEvent, Ticket, TicketComment, TicketEvent, TicketReviewRound } from '../../../api/client';
import { formatStatusLabel } from '../constants';
import { EDIT_ACTIONS, type UserNames } from '../events';

/* Moved unchanged from the old TicketDetail.tsx: one chronological activity
 * timeline (comments, trusted review verdicts, Jira sync events and the
 * ticket's own event log). Only `export` keywords were added; the row
 * components are in TimelineRow.tsx. */

/* ── Timeline ── */

/** Delivery of a comment's outbound push to Jira, folded onto the comment. */
export type JiraDelivery = JiraSyncEvent;

export type TimelineItem =
  | { key: string; at: string; kind: 'event'; text: ReactNode }
  | { key: string; at: string; kind: 'ticketEvent'; event: TicketEvent; names: UserNames }
  | { key: string; at: string; kind: 'more' }
  | { key: string; at: string; kind: 'comment'; comment: TicketComment; verdict?: TicketReviewRound; delivery?: JiraDelivery }
  | { key: string; at: string; kind: 'sync'; event: JiraSyncEvent };

/** Inbound comments and field edits already arrive as ticket comments (a
 *  mirrored Jira comment or a system note), so a successful ledger row for
 *  them would only repeat it. Failed, refused or skipped ones stay visible. */
function duplicatesAComment(e: JiraSyncEvent): boolean {
  if (e.direction !== 'inbound' || (e.kind !== 'comment' && e.kind !== 'field_edit')) return false;
  return e.applied !== false && !e.error && e.state !== 'skipped' && e.state !== 'exhausted' && e.state !== 'pending';
}

function outboundCommentId(e: JiraSyncEvent): string | null {
  if (e.direction !== 'outbound' || e.kind !== 'comment') return null;
  const id = e.payload?.comment_id;
  return typeof id === 'string' && id ? id : null;
}

/** Names the page already knows, by user id. */
export function collectNames(t: Ticket, events: TicketEvent[]): UserNames {
  const m: UserNames = new Map();
  const add = (id: string | null | undefined, name: string | null | undefined) => {
    if (id && name?.trim() && !m.has(id)) m.set(id, name.trim());
  };
  add(t.assigned_user?.id, t.assigned_user?.display_name || t.assigned_user?.email);
  add(t.created_by?.user_id, t.created_by?.display_name);
  add(t.resolved_by?.user_id, t.resolved_by?.display_name);
  for (const e of events) add(e.actor.user_id, e.actor.display_name);
  return m;
}

/** Same actor (by user id), within a few seconds: one write's rows. */
function sameMoment(e: TicketEvent, userId: string | null | undefined, at: string): boolean {
  return (
    !!userId && e.actor.user_id === userId && Math.abs(Date.parse(e.created_at) - Date.parse(at)) <= 5_000
  );
}

/** Fields whose events say everything the update_ticket diff line says. */
const DIFF_FIELDS_SHOWN_BY_EVENTS = new Set([
  'due_date',
  'estimated_effort',
  'assigned_to',
  'assigned_user_id',
  'priority',
  'kind',
  'title',
]);

/** The diff comment update_ticket posts beside its events
 *  (services/ticket_writes.format_edit_diff_summary). Hidden only when it
 *  IS that diff and every field it lists has an event from the same write
 *  that shows the whole change. Anything else stays: other system notes
 *  (e.g. "needs approval again"), comments that merely use the "system"
 *  persona, diffs of description / acceptance criteria / dependencies or a
 *  long title (the event only says it changed), and diffs carrying the
 *  unfenced-edit note. */
function repeatsAnEvent(c: TicketComment, edits: TicketEvent[], names: UserNames): boolean {
  if (c.author_persona !== 'system' || c.source === 'jira') return false;
  const text = c.content ?? '';
  if (!text.startsWith('**Ticket fields updated**')) return false;
  if (text.includes('_Unfenced edit_')) return false;
  const fields = [...text.matchAll(/^- `([a-z_]+)`:/gm)].map((m) => m[1]);
  if (fields.length === 0) return false;
  return fields.every((field) => {
    if (!DIFF_FIELDS_SHOWN_BY_EVENTS.has(field)) return false;
    return edits.some(
      (e) =>
        e.field === field &&
        sameMoment(e, c.author_user_id, c.created_at) &&
        // A long title arrives as a length stub: the event can't show it.
        (field !== 'title' || typeof e.after === 'string') &&
        // The diff names people; the event only has ids. Fold only when
        // every id resolves, or the reader would lose who it was.
        (field !== 'assigned_user_id' || [e.before, e.after].every((v) => userIdsResolve(v, names))),
    );
  });
}

function userIdsResolve(v: unknown, names: UserNames): boolean {
  if (v === null || v === undefined) return true;
  const uid = typeof v === 'object' ? (v as { user_id?: unknown }).user_id : undefined;
  return typeof uid === 'string' && names.has(uid);
}

/** approve / dismiss record a status move and the verdict together; show
 *  the verdict sentence only. */
function partOfModeration(e: TicketEvent, log: TicketEvent[]): boolean {
  if (e.action !== 'status_changed') return false;
  const verdict = e.after === 'open' ? 'approved' : e.after === 'cancelled' ? 'dismissed' : null;
  if (!verdict) return false;
  return log.some(
    (o) =>
      o.action === verdict &&
      (o.actor.user_id ? sameMoment(o, e.actor.user_id, e.created_at) : Math.abs(Date.parse(o.created_at) - Date.parse(e.created_at)) <= 5_000),
  );
}

const RESOLVED_STATUSES = new Set(['done', 'closed', 'cancelled']);

export function buildTimeline(
  t: Ticket,
  comments: TicketComment[],
  events: JiraSyncEvent[],
  rounds: TicketReviewRound[],
  log: { events: TicketEvent[]; hasMore: boolean; names: UserNames } = {
    events: [],
    hasMore: false,
    names: new Map(),
  },
): TimelineItem[] {
  const edits = log.events.filter((e) => EDIT_ACTIONS.has(e.action));
  const verdicts = new Map(rounds.map((r) => [r.comment_id, r]));
  const commentIds = new Set(comments.map((c) => c.id));
  // The ledger is newest-first; walk it oldest-first so events that share a
  // timestamp keep their real order, and the latest delivery per comment wins.
  const chronological = [...events].reverse();
  const deliveries = new Map<string, JiraDelivery>();
  for (const e of chronological) {
    const cid = outboundCommentId(e);
    if (cid && commentIds.has(cid)) deliveries.set(cid, e);
  }

  const items: TimelineItem[] = [];
  if (t.created_at) {
    // Always from the ticket itself, so it shows even before the event log
    // has paged back to the "created" event (which is skipped below).
    const creator =
      t.created_by?.display_name?.trim() || t.created_by?.persona?.trim() || t.created_by_persona?.trim();
    items.push({
      key: 'created',
      at: t.created_at,
      kind: 'event',
      text: creator ? (
        <>
          Created by <span className="font-medium text-text-secondary">{creator}</span>
        </>
      ) : (
        'Created'
      ),
    });
  }
  // The event log, oldest first; the "load earlier" marker goes just
  // before the oldest loaded event.
  const chronologicalLog = [...log.events].reverse();
  if (log.hasMore && chronologicalLog.length > 0) {
    const oldest = Date.parse(chronologicalLog[0].created_at);
    items.push({
      key: 'more',
      at: Number.isNaN(oldest) ? '' : new Date(oldest - 1).toISOString(),
      kind: 'more',
    });
  }
  for (const e of chronologicalLog) {
    if (e.action === 'created') continue;
    if (partOfModeration(e, log.events)) continue;
    items.push({ key: `te:${e.id}`, at: e.created_at, kind: 'ticketEvent', event: e, names: log.names });
  }
  for (const c of comments) {
    if (repeatsAnEvent(c, edits, log.names)) continue;
    items.push({
      key: `c:${c.id}`,
      at: c.created_at,
      kind: 'comment',
      comment: c,
      verdict: verdicts.get(c.id),
      delivery: deliveries.get(c.id),
    });
  }
  for (const e of chronological) {
    if (duplicatesAComment(e)) continue;
    const cid = outboundCommentId(e);
    if (cid && commentIds.has(cid)) continue; // shown on its comment
    items.push({ key: `s:${e.id}`, at: e.created_at ?? '', kind: 'sync', event: e });
  }
  // The ticket's own resolution stamp, unless the log already shows the move.
  const logShowsResolution = log.events.some(
    (e) => e.action === 'status_changed' && typeof e.after === 'string' && RESOLVED_STATUSES.has(e.after),
  );
  if (t.resolved_at && !logShowsResolution) {
    const resolver = t.resolved_by?.display_name?.trim();
    items.push({
      key: 'resolved',
      at: t.resolved_at,
      kind: 'event',
      text: resolver ? (
        <>
          Resolved by <span className="font-medium text-text-secondary">{resolver}</span> ({formatStatusLabel(t.status)})
        </>
      ) : (
        `Resolved (${formatStatusLabel(t.status)})`
      ),
    });
  }
  // Stable chronological order; undated rows keep their place at the end.
  return items
    .map((item, i) => ({ item, i }))
    .sort((a, b) => {
      const ta = a.item.at ? Date.parse(a.item.at) : Infinity;
      const tb = b.item.at ? Date.parse(b.item.at) : Infinity;
      return ta === tb ? a.i - b.i : ta - tb;
    })
    .map(({ item }) => item);
}


export type ActivityFilter = 'all' | 'comments' | 'history';

/** A person's comment (or a verdict); system notes count as history. */
function isComment(item: TimelineItem): boolean {
  return item.kind === 'comment' && item.comment.author_persona !== 'system';
}

export function filterTimeline(items: TimelineItem[], filter: ActivityFilter): TimelineItem[] {
  if (filter === 'all') return items;
  return items.filter((i) => (filter === 'comments' ? isComment(i) : !isComment(i)));
}
