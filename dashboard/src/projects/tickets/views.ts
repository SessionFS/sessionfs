import type { Ticket } from '../../api/client';
import { STATUS_LABEL } from './constants';
import { TERMINAL_STATUSES, formatDueDate, todayIso } from './schedule';
import { ticketPersonName } from './assignee';

/* ── Ticket list model ──
 *
 * Pure functions behind the Tickets list: saved views, quick filters,
 * search, grouping, sorting and the row signals. The page fetches the whole
 * project list once and does all of this client-side (the server list has no
 * search, sort or paging yet), which is fine up to about a thousand tickets. */

export type ViewId = 'mine' | 'triage' | 'review' | 'blocked' | 'overdue' | 'all';
export type GroupBy = 'status' | 'issue' | 'assignee' | 'none';
export type SortBy = 'priority' | 'due' | 'updated';

export interface ListParams {
  view: ViewId;
  group: GroupBy;
  sort: SortBy;
  q: string;
  issuesOnly: boolean;
  highOnly: boolean;
  showFinished: boolean;
}

export const DEFAULT_PARAMS: ListParams = {
  view: 'all',
  group: 'status',
  sort: 'priority',
  q: '',
  issuesOnly: false,
  highOnly: false,
  showFinished: false,
};

const VIEW_IDS: ViewId[] = ['mine', 'triage', 'review', 'blocked', 'overdue', 'all'];
const GROUP_IDS: GroupBy[] = ['status', 'issue', 'assignee', 'none'];
const SORT_IDS: SortBy[] = ['priority', 'due', 'updated'];

/** List state from the page URL. Unknown values fall back to the defaults
 *  (another sub-tab may have left its own `?view=` behind). The legacy
 *  `?assignee=me` and `?due=overdue` links still land on Mine / Overdue. */
export function parseListParams(get: (key: string) => string | null): ListParams {
  const view = get('view');
  const group = get('group');
  const sort = get('sort');
  let resolvedView: ViewId = VIEW_IDS.includes(view as ViewId) ? (view as ViewId) : 'all';
  if (!VIEW_IDS.includes(view as ViewId)) {
    if (get('assignee') === 'me') resolvedView = 'mine';
    else if (get('due') === 'overdue') resolvedView = 'overdue';
  }
  return {
    view: resolvedView,
    group: GROUP_IDS.includes(group as GroupBy) ? (group as GroupBy) : 'status',
    sort: SORT_IDS.includes(sort as SortBy) ? (sort as SortBy) : 'priority',
    q: get('q') ?? '',
    issuesOnly: get('issues') === '1',
    highOnly: get('high') === '1',
    showFinished: get('finished') === '1',
  };
}

/** The URL keys owned by the Tickets list (cleared when leaving it). */
export const LIST_PARAM_KEYS = ['view', 'group', 'sort', 'q', 'issues', 'high', 'finished', 'assignee', 'due'];

/** Write `p` into `params`, leaving defaults out so plain links stay short. */
export function writeListParams(params: URLSearchParams, p: ListParams): URLSearchParams {
  const set = (key: string, value: string | null) => {
    if (value === null) params.delete(key);
    else params.set(key, value);
  };
  set('view', p.view === DEFAULT_PARAMS.view ? null : p.view);
  set('group', p.group === DEFAULT_PARAMS.group ? null : p.group);
  set('sort', p.sort === DEFAULT_PARAMS.sort ? null : p.sort);
  set('q', p.q ? p.q : null);
  set('issues', p.issuesOnly ? '1' : null);
  set('high', p.highOnly ? '1' : null);
  set('finished', p.showFinished ? '1' : null);
  // Superseded by ?view=.
  params.delete('assignee');
  params.delete('due');
  return params;
}

export const isFinished = (t: Pick<Ticket, 'status'>) => TERMINAL_STATUSES.has(t.status);

export type ById = Map<string, Ticket>;

export function indexById(list: Ticket[]): ById {
  return new Map(list.map((t) => [t.id, t]));
}

/** Whole days from today (UTC, like the server's overdue rule) to a
 *  "YYYY-MM-DD" due date; negative when past. */
export function daysUntil(due: string, today: string = todayIso()): number {
  const a = Date.UTC(+today.slice(0, 4), +today.slice(5, 7) - 1, +today.slice(8, 10));
  const b = Date.UTC(+due.slice(0, 4), +due.slice(5, 7) - 1, +due.slice(8, 10));
  return Math.round((b - a) / 86_400_000);
}

export function isLate(t: Pick<Ticket, 'status' | 'due_date'>, today: string = todayIso()): boolean {
  return !!t.due_date && !isFinished(t) && daysUntil(t.due_date, today) < 0;
}

/** Unfinished dependencies: the server count when present, else counted from
 *  the list (older servers). */
export function openBlockers(t: Ticket, byId: ById): number {
  if (typeof t.open_dependency_count === 'number') return t.open_dependency_count;
  return t.depends_on.filter((id) => {
    const dep = byId.get(id);
    return !!dep && !isFinished(dep);
  }).length;
}

/** Child Tasks of an Issue: the server rollup when present, else counted. */
export function childRollup(t: Ticket, byId: ById): { total: number; finished: number } {
  if (t.child_counts) return t.child_counts;
  const kids = t.child_ticket_ids.map((id) => byId.get(id)).filter((k): k is Ticket => !!k);
  return { total: t.child_ticket_ids.length, finished: kids.filter(isFinished).length };
}

export function isMine(t: Ticket, userId: string | null): boolean {
  return !!userId && t.assigned_user_id === userId;
}

export function isBlocked(t: Ticket, byId: ById): boolean {
  if (t.status === 'blocked') return true;
  return !isFinished(t) && t.status !== 'review' && openBlockers(t, byId) > 0;
}

export interface ViewDef {
  id: ViewId;
  label: string;
  /** Count tone: `alert` for Blocked/Overdue, `review` for Needs review. */
  tone?: 'alert' | 'review';
  test: (t: Ticket, ctx: { userId: string | null; byId: ById; today: string }) => boolean;
  empty: [string, string];
}

export const VIEWS: ViewDef[] = [
  {
    id: 'mine',
    label: 'Mine',
    test: (t, c) => isMine(t, c.userId) && !isFinished(t),
    empty: ['Nothing assigned to you', 'Tickets assigned to you as a person show up here. Work for an agent you represent is under All.'],
  },
  {
    id: 'triage',
    label: 'Triage',
    test: (t) => t.status === 'suggested',
    empty: ['No suggestions to triage', 'A ticket an agent files starts as Suggested. Approve it to open it, or dismiss it.'],
  },
  {
    id: 'review',
    label: 'Needs review',
    tone: 'review',
    test: (t) => t.status === 'review',
    empty: ['Nothing waiting on review', 'Tickets in review show up here with their latest trusted verdict.'],
  },
  {
    id: 'blocked',
    label: 'Blocked',
    tone: 'alert',
    test: (t, c) => isBlocked(t, c.byId),
    empty: ['Nothing blocked', 'Blocked tickets, and tickets waiting on an unfinished dependency, show up here.'],
  },
  {
    id: 'overdue',
    label: 'Overdue',
    tone: 'alert',
    test: (t, c) => isLate(t, c.today),
    empty: ['Nothing overdue', 'Tickets past their due date that aren’t done, closed or cancelled show up here.'],
  },
  {
    id: 'all',
    label: 'All',
    test: () => true,
    empty: ['No tickets', ''],
  },
];

export function viewDef(id: ViewId): ViewDef {
  return VIEWS.find((v) => v.id === id) ?? VIEWS[VIEWS.length - 1];
}

/** Per-view counts over the whole list. All leaves finished tickets out,
 *  like the list does until "Show finished" is on. */
export function viewCounts(list: Ticket[], userId: string | null, today: string = todayIso()): Record<ViewId, number> {
  const byId = indexById(list);
  const ctx = { userId, byId, today };
  const out = {} as Record<ViewId, number>;
  for (const v of VIEWS) {
    out[v.id] = list.filter((t) => v.test(t, ctx) && (v.id !== 'all' || !isFinished(t))).length;
  }
  return out;
}

/** Display name of a ticket's assignee, or null when unassigned. */
export function assigneeName(t: Ticket): string | null {
  if (t.assigned_user_id) return ticketPersonName(t);
  if (t.assigned_to) return t.assigned_to;
  if (t.assigned_contact) return t.assigned_contact.display_name;
  return null;
}

export function jiraKey(t: Ticket): string | null {
  return t.jira_key ?? t.jira?.issue_key ?? null;
}

function matchesSearch(t: Ticket, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  const hay = [t.id, t.title, t.description, assigneeName(t), t.assigned_user?.email, jiraKey(t)]
    .filter(Boolean)
    .join('\n')
    .toLowerCase();
  return hay.includes(needle);
}

/** Tickets in the current view after the quick filters and the search. */
export function visibleTickets(
  list: Ticket[],
  p: ListParams,
  userId: string | null,
  today: string = todayIso(),
): Ticket[] {
  const byId = indexById(list);
  const v = viewDef(p.view);
  const ctx = { userId, byId, today };
  return list.filter(
    (t) =>
      v.test(t, ctx) &&
      (!p.issuesOnly || t.kind === 'issue') &&
      (!p.highOnly || t.priority === 'high' || t.priority === 'critical') &&
      (p.showFinished || p.view !== 'all' || !isFinished(t)) &&
      matchesSearch(t, p.q),
  );
}

/** Finished tickets hidden from All while "Show finished" is off. */
export function hiddenFinishedCount(list: Ticket[], p: ListParams): number {
  if (p.view !== 'all' || p.showFinished) return 0;
  const probe = { ...p, showFinished: true };
  return visibleTickets(list, probe, null).filter(isFinished).length;
}

const PRIORITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
const rank = (p: string) => PRIORITY_RANK[p] ?? 4;
const dueKey = (t: Ticket) => t.due_date ?? '9999-99-99';

export function compareTickets(sort: SortBy, today: string = todayIso()) {
  return (a: Ticket, b: Ticket): number => {
    if (sort === 'due') {
      return dueKey(a).localeCompare(dueKey(b)) || rank(a.priority) - rank(b.priority) || a.id.localeCompare(b.id);
    }
    if (sort === 'updated') {
      return b.updated_at.localeCompare(a.updated_at) || a.id.localeCompare(b.id);
    }
    return (
      rank(a.priority) - rank(b.priority) ||
      Number(isLate(b, today)) - Number(isLate(a, today)) ||
      dueKey(a).localeCompare(dueKey(b)) ||
      b.updated_at.localeCompare(a.updated_at) ||
      a.id.localeCompare(b.id)
    );
  };
}

export function sortTickets(list: Ticket[], sort: SortBy, today: string = todayIso()): Ticket[] {
  return [...list].sort(compareTickets(sort, today));
}

export const STATUS_ORDER = ['blocked', 'review', 'in_progress', 'open', 'suggested', 'done', 'closed', 'cancelled'];

export interface RowItem {
  ticket: Ticket;
  /** A child Task shown under its Issue (group by Issue). */
  child?: boolean;
}

export interface TicketGroup {
  key: string;
  label: string;
  /** What the header icon shows. */
  icon: { kind: 'status'; status: string } | { kind: 'assignee'; ticket: Ticket | null } | null;
  /** Quiet text after the count ("1/4 done"). */
  sub?: string;
  rows: RowItem[];
}

export function groupTickets(
  list: Ticket[],
  by: GroupBy,
  sort: SortBy,
  { userId, all, today = todayIso() }: { userId: string | null; all: Ticket[]; today?: string },
): TicketGroup[] {
  const cmp = compareTickets(sort, today);
  const sorted = [...list].sort(cmp);
  if (by === 'none') {
    return [{ key: 'all', label: 'All tickets', icon: null, rows: sorted.map((ticket) => ({ ticket })) }];
  }
  if (by === 'status') {
    const known = STATUS_ORDER.map((s) => ({
      key: s,
      label: STATUS_LABEL[s] ?? s,
      icon: { kind: 'status' as const, status: s },
      rows: sorted.filter((t) => t.status === s).map((ticket) => ({ ticket })),
    }));
    const other = sorted.filter((t) => !STATUS_ORDER.includes(t.status));
    const groups: TicketGroup[] = known;
    if (other.length) groups.push({ key: 'other', label: 'Other', icon: null, rows: other.map((ticket) => ({ ticket })) });
    return groups.filter((g) => g.rows.length > 0);
  }
  if (by === 'assignee') {
    const groups = new Map<string, TicketGroup>();
    for (const t of sorted) {
      const key = t.assigned_user_id
        ? `user:${t.assigned_user_id}`
        : t.assigned_to
          ? `persona:${t.assigned_to}`
          : t.assigned_contact_id
            ? `contact:${t.assigned_contact_id}`
            : 'none';
      if (!groups.has(key)) {
        const name = assigneeName(t);
        const you = !!userId && t.assigned_user_id === userId;
        groups.set(key, {
          key,
          label: name ? (you ? `${name} (you)` : name) : 'Unassigned',
          icon: { kind: 'assignee', ticket: key === 'none' ? null : t },
          sub: t.assigned_to && !t.assigned_user_id ? 'agent' : undefined,
          rows: [],
        });
      }
      groups.get(key)!.rows.push({ ticket: t });
    }
    const mine = userId ? `user:${userId}` : null;
    return [...groups.values()].sort((a, b) => {
      if (a.key === mine) return -1;
      if (b.key === mine) return 1;
      if (a.key === 'none') return 1;
      if (b.key === 'none') return -1;
      return b.rows.length - a.rows.length || a.label.localeCompare(b.label);
    });
  }
  // By Issue: each Issue, then its child Tasks in this list; then the rest.
  const byId = indexById(all);
  const out = new Map<string, TicketGroup>();
  const ensure = (issueId: string) => {
    let g = out.get(issueId);
    if (!g) {
      const issue = byId.get(issueId);
      const roll = issue ? childRollup(issue, byId) : null;
      g = {
        key: issueId,
        label: issue?.title ?? issueId,
        icon: issue ? { kind: 'status', status: issue.status } : null,
        sub: roll && roll.total ? `${roll.finished}/${roll.total} done` : undefined,
        rows: [],
      };
      out.set(issueId, g);
    }
    return g;
  };
  // Issues in sort order first, so group order follows the sort.
  for (const t of sorted) if (t.kind === 'issue') ensure(t.id).rows.push({ ticket: t });
  const loose: RowItem[] = [];
  for (const t of sorted) {
    if (t.kind === 'issue') continue;
    if (t.parent_ticket_id && byId.has(t.parent_ticket_id)) ensure(t.parent_ticket_id).rows.push({ ticket: t, child: true });
    else loose.push({ ticket: t });
  }
  const groups = [...out.values()];
  if (loose.length) groups.push({ key: 'loose', label: 'Not in an Issue', icon: null, rows: loose });
  return groups;
}

/* ── Row text ── */

export interface DueDisplay {
  text: string;
  tone: 'late' | 'soon' | null;
  /** Full date for a tooltip / screen readers. */
  full: string;
}

export function dueDisplay(t: Pick<Ticket, 'status' | 'due_date'>, today: string = todayIso()): DueDisplay | null {
  if (!t.due_date) return null;
  const full = formatDueDate(t.due_date);
  if (isFinished(t)) return { text: full, tone: null, full };
  const n = daysUntil(t.due_date, today);
  if (n < 0) return { text: `${-n}d late`, tone: 'late', full };
  if (n === 0) return { text: 'Today', tone: 'soon', full };
  if (n === 1) return { text: 'Tomorrow', tone: 'soon', full };
  if (n <= 7) return { text: `In ${n}d`, tone: n <= 2 ? 'soon' : null, full };
  return { text: full, tone: null, full };
}

/** Compact age: "now", "5h", "3d", "4mo". */
export function shortAge(iso: string, now: Date = new Date()): string {
  const ms = now.getTime() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return '';
  const h = Math.floor(ms / 3_600_000);
  if (h < 1) return 'now';
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 60) return `${d}d`;
  return `${Math.floor(d / 30)}mo`;
}

export type ReviewSignal =
  | { kind: 'clean'; round: number }
  | { kind: 'changes'; round: number; open: number }
  | { kind: 'waiting' };

/** The review pill: latest trusted verdict, or "Awaiting review" for a
 *  ticket in review that has none. Nothing for finished tickets. */
export function reviewSignal(t: Ticket): ReviewSignal | null {
  if (isFinished(t)) return null;
  const s = t.review_summary;
  if (s && s.last_verdict) {
    return s.last_verdict === 'clean'
      ? { kind: 'clean', round: s.last_round }
      : { kind: 'changes', round: s.last_round, open: s.open_findings };
  }
  return t.status === 'review' ? { kind: 'waiting' } : null;
}

export function reviewSignalText(s: ReviewSignal): string {
  if (s.kind === 'clean') return `R${s.round} clean`;
  if (s.kind === 'changes') return `R${s.round} · ${s.open} open`;
  return 'Awaiting review';
}

export function reviewSignalTitle(s: ReviewSignal): string {
  if (s.kind === 'clean') return `Round ${s.round}: verified clean by a trusted reviewer`;
  if (s.kind === 'changes') {
    return `Round ${s.round}: changes requested, ${s.open} open finding${s.open === 1 ? '' : 's'}`;
  }
  return 'In review, no trusted verdict yet';
}

/** The moves the dashboard can make on a ticket, by status and kind. The
 *  viewer's standing is checked separately (viewer.ts). */
export function dashboardMoves(t: Pick<Ticket, 'status' | 'kind'>): Array<'approve' | 'dismiss' | 'close'> {
  const out: Array<'approve' | 'dismiss' | 'close'> = [];
  if (t.status === 'suggested') out.push('approve');
  if (t.status === 'suggested' || t.status === 'open') out.push('dismiss');
  if (t.kind === 'issue' && t.status === 'in_progress') out.push('close');
  return out;
}

export function priorityText(priority: string): string {
  return priority.charAt(0).toUpperCase() + priority.slice(1);
}

/* ── Board ── */

export interface BoardColumn {
  key: string;
  label: string;
  /** The status the column header's icon shows. */
  status: string;
  statuses: string[];
  note?: string;
}

/** Six columns. Done holds closed Issues too; Cancelled is never a column
 *  (the list shows it with "Show finished"). */
export const BOARD_COLUMNS: BoardColumn[] = [
  { key: 'triage', label: 'Triage', status: 'suggested', statuses: ['suggested'], note: 'Agent suggestions waiting for approval' },
  { key: 'open', label: 'Open', status: 'open', statuses: ['open'] },
  { key: 'in_progress', label: 'In progress', status: 'in_progress', statuses: ['in_progress'], note: 'Agents move tickets here from the CLI' },
  { key: 'blocked', label: 'Blocked', status: 'blocked', statuses: ['blocked'] },
  { key: 'review', label: 'In review', status: 'review', statuses: ['review'] },
  { key: 'done', label: 'Done', status: 'done', statuses: ['done', 'closed'] },
];

export function boardColumns(
  list: Ticket[],
  sort: SortBy,
  today: string = todayIso(),
): { column: BoardColumn; tickets: Ticket[] }[] {
  const cmp = compareTickets(sort, today);
  return BOARD_COLUMNS.map((column) => ({
    column,
    tickets: list.filter((t) => column.statuses.includes(t.status)).sort(cmp),
  }));
}

/** The board's tickets in reading order (column by column), for j / k and
 *  the pane's previous / next. */
export function boardOrder(list: Ticket[], sort: SortBy, today: string = todayIso()): Ticket[] {
  return boardColumns(list, sort, today).flatMap((c) => c.tickets);
}
