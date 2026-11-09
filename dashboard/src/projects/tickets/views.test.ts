import { describe, expect, it } from 'vitest';
import type { Ticket } from '../../api/client';
import {
  BOARD_COLUMNS,
  DEFAULT_PARAMS,
  boardColumns,
  boardOrder,
  childRollup,
  compareTickets,
  dashboardMoves,
  daysUntil,
  dueDisplay,
  groupTickets,
  hiddenFinishedCount,
  indexById,
  openBlockers,
  parseListParams,
  reviewSignal,
  reviewSignalText,
  shortAge,
  sortTickets,
  viewCounts,
  visibleTickets,
  writeListParams,
  type ListParams,
} from './views';

const TODAY = '2026-11-08';

function t(over: Partial<Ticket> & { id: string }): Ticket {
  return {
    project_id: 'p',
    title: over.id,
    description: '',
    priority: 'medium',
    assigned_to: null,
    assigned_user_id: null,
    created_by_user_id: 'u1',
    created_by_session_id: null,
    created_by_persona: null,
    status: 'open',
    kind: 'task',
    parent_ticket_id: null,
    child_ticket_ids: [],
    context_refs: [],
    file_refs: [],
    related_sessions: [],
    acceptance_criteria: [],
    resolver_session_id: null,
    resolver_user_id: null,
    completion_notes: null,
    changed_files: [],
    knowledge_entry_ids: [],
    depends_on: [],
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
    resolved_at: null,
    ...over,
  } as Ticket;
}

const params = (over: Partial<ListParams> = {}): ListParams => ({ ...DEFAULT_PARAMS, ...over });
const ids = (list: Ticket[]) => list.map((x) => x.id);

const LIST: Ticket[] = [
  t({ id: 'sug', status: 'suggested' }),
  t({ id: 'mine', status: 'in_progress', assigned_user_id: 'me', due_date: '2026-11-10' }),
  t({ id: 'mine_done', status: 'done', assigned_user_id: 'me' }),
  t({ id: 'rev', status: 'review', depends_on: ['open_dep'] }),
  t({ id: 'blk', status: 'blocked' }),
  t({ id: 'waits', status: 'open', depends_on: ['open_dep', 'mine_done'] }),
  t({ id: 'open_dep', status: 'open', due_date: '2026-11-01', priority: 'high' }),
  t({ id: 'late_done', status: 'done', due_date: '2026-11-01' }),
  t({ id: 'cancel', status: 'cancelled' }),
];

describe('saved views', () => {
  it('match the prototype predicates', () => {
    const v = (view: ListParams['view']) => ids(visibleTickets(LIST, params({ view }), 'me', TODAY));
    expect(v('mine')).toEqual(['mine']);
    expect(v('triage')).toEqual(['sug']);
    expect(v('review')).toEqual(['rev']);
    // A review ticket is never "blocked" by its dependencies; a finished one neither.
    expect(v('blocked')).toEqual(['blk', 'waits']);
    expect(v('overdue')).toEqual(['open_dep']);
    expect(v('all')).toEqual(['sug', 'mine', 'rev', 'blk', 'waits', 'open_dep']);
  });

  it('counts each view, All without the finished ones', () => {
    expect(viewCounts(LIST, 'me', TODAY)).toEqual({ mine: 1, triage: 1, review: 1, blocked: 2, overdue: 1, all: 6 });
    expect(viewCounts(LIST, null, TODAY).mine).toBe(0);
  });

  it('folds finished tickets in All until Show finished', () => {
    expect(hiddenFinishedCount(LIST, params())).toBe(3);
    expect(hiddenFinishedCount(LIST, params({ showFinished: true }))).toBe(0);
    expect(hiddenFinishedCount(LIST, params({ view: 'mine' }))).toBe(0);
    expect(ids(visibleTickets(LIST, params({ showFinished: true }), 'me', TODAY))).toContain('late_done');
  });

  it('applies the quick filters and search', () => {
    const list = [
      t({ id: 'tk_a', title: 'Fix webhook', kind: 'issue', priority: 'critical' }),
      t({ id: 'tk_b', title: 'Other', description: 'mentions WEBHOOK', priority: 'low' }),
      t({ id: 'tk_c', title: 'Agent work', assigned_to: 'atlas', jira_key: 'WID-9' }),
    ];
    expect(ids(visibleTickets(list, params({ issuesOnly: true }), null))).toEqual(['tk_a']);
    expect(ids(visibleTickets(list, params({ highOnly: true }), null))).toEqual(['tk_a']);
    expect(ids(visibleTickets(list, params({ q: 'webhook' }), null))).toEqual(['tk_a', 'tk_b']);
    expect(ids(visibleTickets(list, params({ q: 'atlas' }), null))).toEqual(['tk_c']);
    expect(ids(visibleTickets(list, params({ q: 'wid-9' }), null))).toEqual(['tk_c']);
    expect(ids(visibleTickets(list, params({ q: 'TK_B' }), null))).toEqual(['tk_b']);
  });
});

describe('URL params', () => {
  const get = (q: string) => {
    const sp = new URLSearchParams(q);
    return (k: string) => sp.get(k);
  };

  it('parses known values and falls back on unknown ones', () => {
    expect(parseListParams(get('view=review&group=issue&sort=due&q=x&issues=1&high=1&finished=1'))).toEqual({
      view: 'review', group: 'issue', sort: 'due', q: 'x', issuesOnly: true, highOnly: true, showFinished: true,
    });
    // Another sub-tab's ?view=board is not a ticket view.
    expect(parseListParams(get('view=board&group=zzz&sort=zzz'))).toEqual(DEFAULT_PARAMS);
  });

  it('maps the old ?assignee=me and ?due=overdue links', () => {
    expect(parseListParams(get('assignee=me')).view).toBe('mine');
    expect(parseListParams(get('due=overdue')).view).toBe('overdue');
    expect(parseListParams(get('view=triage&assignee=me')).view).toBe('triage');
  });

  it('writes only non-defaults and drops the old keys', () => {
    const sp = writeListParams(new URLSearchParams('tab=work&assignee=me&due=overdue&view=mine'), params({ group: 'assignee' }));
    expect(sp.toString()).toBe('tab=work&group=assignee');
    const round = writeListParams(new URLSearchParams(), params({ view: 'blocked', q: 'a b', showFinished: true }));
    expect(parseListParams((k) => round.get(k))).toEqual(params({ view: 'blocked', q: 'a b', showFinished: true }));
  });
});

describe('sorting', () => {
  const list = [
    t({ id: 'a', priority: 'low', updated_at: '2026-11-07T00:00:00Z' }),
    t({ id: 'b', priority: 'high', due_date: '2026-11-20', updated_at: '2026-11-02T00:00:00Z' }),
    t({ id: 'c', priority: 'high', due_date: '2026-11-01', updated_at: '2026-11-03T00:00:00Z' }),
    t({ id: 'd', priority: 'critical', updated_at: '2026-11-01T00:00:00Z' }),
  ];
  it('by priority, late first, then due', () => {
    expect(ids(sortTickets(list, 'priority', TODAY))).toEqual(['d', 'c', 'b', 'a']);
  });
  it('by due date, undated last', () => {
    expect(ids(sortTickets(list, 'due', TODAY))).toEqual(['c', 'b', 'd', 'a']);
  });
  it('by most recently updated', () => {
    expect(ids(sortTickets(list, 'updated', TODAY))).toEqual(['a', 'c', 'b', 'd']);
  });
  it('is a total order (ties break on id)', () => {
    const cmp = compareTickets('priority', TODAY);
    expect(cmp(t({ id: 'x' }), t({ id: 'y' }))).toBeLessThan(0);
  });
});

describe('grouping', () => {
  it('by status in the fixed order, empty groups left out', () => {
    const groups = groupTickets(LIST, 'status', 'priority', { userId: 'me', all: LIST, today: TODAY });
    expect(groups.map((g) => g.key)).toEqual(['blocked', 'review', 'in_progress', 'open', 'suggested', 'done', 'cancelled']);
    expect(groups.find((g) => g.key === 'open')!.label).toBe('Open');
  });

  it('by Issue: the Issue first, then its children, then loose tasks', () => {
    const issue = t({ id: 'iss', kind: 'issue', status: 'in_progress', child_ticket_ids: ['c1', 'c2', 'c3'] });
    const all = [
      issue,
      t({ id: 'c1', parent_ticket_id: 'iss', status: 'done' }),
      t({ id: 'c2', parent_ticket_id: 'iss', priority: 'high' }),
      t({ id: 'c3', parent_ticket_id: 'iss' }),
      t({ id: 'loose' }),
      // The parent Issue is filtered out of the list, but the group still names it.
      t({ id: 'iss2', kind: 'issue', title: 'Hidden issue', status: 'blocked' }),
      t({ id: 'c4', parent_ticket_id: 'iss2' }),
    ];
    const shown = all.filter((x) => x.id !== 'c1' && x.id !== 'iss2');
    const groups = groupTickets(shown, 'issue', 'priority', { userId: null, all, today: TODAY });
    expect(groups.map((g) => g.key)).toEqual(['iss', 'iss2', 'loose']);
    expect(groups[0].rows.map((r) => [r.ticket.id, !!r.child])).toEqual([['iss', false], ['c2', true], ['c3', true]]);
    expect(groups[0].sub).toBe('1/3 done');
    expect(groups[1].label).toBe('Hidden issue');
    expect(groups[2].label).toBe('Not in an Issue');
  });

  it('by assignee: you first, unassigned last', () => {
    const list = [
      t({ id: 'n1' }),
      t({ id: 'a1', assigned_to: 'atlas' }),
      t({ id: 'a2', assigned_to: 'atlas' }),
      t({ id: 'm1', assigned_user_id: 'me', assigned_user: { id: 'me', display_name: 'Dev', email: 'd@x.test' } }),
    ];
    const groups = groupTickets(list, 'assignee', 'priority', { userId: 'me', all: list, today: TODAY });
    expect(groups.map((g) => g.label)).toEqual(['Dev (you)', 'atlas', 'Unassigned']);
    expect(groups[1].sub).toBe('agent');
  });

  it('none: one group', () => {
    const groups = groupTickets(LIST, 'none', 'priority', { userId: null, all: LIST, today: TODAY });
    expect(groups).toHaveLength(1);
    expect(groups[0].rows).toHaveLength(LIST.length);
  });
});

describe('row signals', () => {
  it('review pill: clean, changes, awaiting, nothing', () => {
    const s = (over: Partial<Ticket>) => reviewSignal(t({ id: 'x', ...over }));
    expect(s({ status: 'review', review_summary: { last_round: 2, last_verdict: 'clean', open_findings: 0, trusted: true } })).toEqual({ kind: 'clean', round: 2 });
    expect(reviewSignalText(s({ status: 'in_progress', review_summary: { last_round: 1, last_verdict: 'changes', open_findings: 2, trusted: true } })!)).toBe('R1 · 2 open');
    expect(reviewSignalText(s({ status: 'review' })!)).toBe('Awaiting review');
    expect(s({ status: 'open' })).toBeNull();
    expect(s({ status: 'done', review_summary: { last_round: 2, last_verdict: 'clean', open_findings: 0, trusted: true } })).toBeNull();
  });

  it('blockers and rollup prefer the server counts, else count the list', () => {
    const byId = indexById(LIST);
    expect(openBlockers(t({ id: 'x', depends_on: ['open_dep', 'mine_done', 'missing'] }), byId)).toBe(1);
    expect(openBlockers(t({ id: 'x', depends_on: ['open_dep'], open_dependency_count: 0 }), byId)).toBe(0);
    const issue = t({ id: 'i', kind: 'issue', child_ticket_ids: ['mine_done', 'open_dep', 'gone'] });
    expect(childRollup(issue, byId)).toEqual({ total: 3, finished: 1 });
    expect(childRollup({ ...issue, child_counts: { total: 5, finished: 4 } }, byId)).toEqual({ total: 5, finished: 4 });
  });

  it('due text', () => {
    expect(daysUntil('2026-11-05', TODAY)).toBe(-3);
    expect(dueDisplay(t({ id: 'x', due_date: '2026-11-05' }), TODAY)).toMatchObject({ text: '3d late', tone: 'late' });
    expect(dueDisplay(t({ id: 'x', due_date: '2026-11-08' }), TODAY)).toMatchObject({ text: 'Today', tone: 'soon' });
    expect(dueDisplay(t({ id: 'x', due_date: '2026-11-09' }), TODAY)).toMatchObject({ text: 'Tomorrow', tone: 'soon' });
    expect(dueDisplay(t({ id: 'x', due_date: '2026-11-10' }), TODAY)).toMatchObject({ text: 'In 2d', tone: 'soon' });
    expect(dueDisplay(t({ id: 'x', due_date: '2026-11-14' }), TODAY)).toMatchObject({ text: 'In 6d', tone: null });
    expect(dueDisplay(t({ id: 'x', due_date: '2026-11-30' }), TODAY)!.text).toMatch(/Nov 30/);
    expect(dueDisplay(t({ id: 'x', status: 'done', due_date: '2026-11-05' }), TODAY)).toMatchObject({ tone: null });
    expect(dueDisplay(t({ id: 'x' }), TODAY)).toBeNull();
  });

  it('age', () => {
    const now = new Date('2026-11-08T12:00:00Z');
    expect(shortAge('2026-11-08T11:30:00Z', now)).toBe('now');
    expect(shortAge('2026-11-08T07:00:00Z', now)).toBe('5h');
    expect(shortAge('2026-11-05T12:00:00Z', now)).toBe('3d');
    expect(shortAge('2026-06-01T12:00:00Z', now)).toBe('5mo');
  });

  it('dashboard moves never include CLI-only transitions', () => {
    expect(dashboardMoves({ status: 'suggested', kind: 'task' })).toEqual(['approve', 'dismiss']);
    expect(dashboardMoves({ status: 'open', kind: 'task' })).toEqual(['dismiss']);
    expect(dashboardMoves({ status: 'in_progress', kind: 'issue' })).toEqual(['close']);
    for (const status of ['in_progress', 'blocked', 'review', 'done', 'closed', 'cancelled']) {
      expect(dashboardMoves({ status, kind: 'task' })).toEqual([]);
    }
  });
});

describe('board columns', () => {
  it('has six columns in order, with Done holding closed and Cancelled left out', () => {
    expect(BOARD_COLUMNS.map((c) => c.label)).toEqual(['Triage', 'Open', 'In progress', 'Blocked', 'In review', 'Done']);
    const list = [
      t({ id: 's', status: 'suggested' }),
      t({ id: 'c', status: 'closed', kind: 'issue' }),
      t({ id: 'd', status: 'done' }),
      t({ id: 'x', status: 'cancelled' }),
      t({ id: 'r', status: 'review' }),
    ];
    const cols = boardColumns(list, 'priority', TODAY);
    expect(cols.map((c) => c.tickets.map((x) => x.id))).toEqual([['s'], [], [], [], ['r'], ['c', 'd']]);
    expect(boardOrder(list, 'priority', TODAY).map((x) => x.id)).toEqual(['s', 'r', 'c', 'd']);
  });

  it('sorts each column with the list sort', () => {
    const list = [t({ id: 'lo', status: 'open', priority: 'low' }), t({ id: 'hi', status: 'open', priority: 'critical' })];
    expect(boardColumns(list, 'priority', TODAY)[1].tickets.map((x) => x.id)).toEqual(['hi', 'lo']);
  });
});
