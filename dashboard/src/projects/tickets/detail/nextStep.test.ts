import { describe, expect, it } from 'vitest';
import type { Ticket, TicketReviewState } from '../../../api/client';
import { childRollup, nextStep, openBlockers, type NextStepPermissions } from './nextStep';

const TODAY = '2026-11-09';

function t(over: Partial<Ticket> = {}): Ticket {
  return {
    id: 'tk_1',
    project_id: 'p',
    title: 'Export sessions',
    description: '',
    priority: 'medium',
    assigned_to: 'atlas',
    created_by_user_id: 'u1',
    created_by_session_id: null,
    created_by_persona: 'compass',
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
  };
}

const ALL: NextStepPermissions = { approve: true, dismiss: true, close: true, reassign: true, editSchedule: true };
const NONE: NextStepPermissions = { approve: false, dismiss: false, close: false, reassign: false, editSchedule: false };

type Review = NonNullable<TicketReviewState['review_state']>;
function review(verdicts: string[], open = 0): Review {
  return {
    open_findings: Array.from({ length: open }, (_, i) => ({ severity: 'MEDIUM', text: `f${i}`, round: 1 })),
    closed_findings: [],
    last_verdict: verdicts[verdicts.length - 1] ?? null,
    rounds: verdicts.map((v, i) => ({
      round: i + 1,
      verdict: v,
      raw_verdict: v,
      is_strict_verified_clean: v === 'VERIFIED-CLEAN',
      comment_id: `c${i}`,
      timestamp: '2026-11-02T00:00:00Z',
      findings_raised: 0,
    })),
  };
}

const kinds = (s: ReturnType<typeof nextStep>) => s?.actions.map((a) => a.kind) ?? [];

describe('nextStep', () => {
  it('asks for approval of a suggested ticket, offering approve and dismiss', () => {
    const s = nextStep({ ticket: t({ status: 'suggested' }), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('needs_approval');
    expect(s.tone).toBe('warning');
    expect(s.text).toMatch(/^compass filed this as \{\{ticket-suggested\|suggested\}\}/);
    expect(kinds(s)).toEqual(['approve', 'dismiss']);
  });

  it('offers nothing to a viewer who may not approve, and says who can', () => {
    const s = nextStep({ ticket: t({ status: 'suggested' }), perms: NONE, today: TODAY })!;
    expect(s.actions).toEqual([]);
    expect(s.text).toMatch(/A project editor makes that call/);
  });

  it('offers to close an Issue whose child tasks are all finished', () => {
    const issue = t({ kind: 'issue', status: 'in_progress', child_ticket_ids: ['a', 'b'] });
    const children = [t({ id: 'a', status: 'done' }), t({ id: 'b', status: 'cancelled' })];
    const s = nextStep({ ticket: issue, children, perms: ALL, today: TODAY })!;
    expect(s.id).toBe('children_finished');
    expect(kinds(s)).toEqual(['close']);
    expect(nextStep({ ticket: issue, children, perms: NONE, today: TODAY })!.actions).toEqual([]);
  });

  it('shows Issue progress and blocked children while work goes on', () => {
    const issue = t({ kind: 'issue', status: 'in_progress', child_ticket_ids: ['a', 'b', 'c'] });
    const children = [t({ id: 'a', status: 'done' }), t({ id: 'b', status: 'blocked' }), t({ id: 'c', status: 'open' })];
    const s = nextStep({ ticket: issue, children, perms: ALL, today: TODAY })!;
    expect(s.id).toBe('issue_progress');
    expect(s.title).toBe('1 of 3 child tasks finished');
    expect(s.text).toMatch(/1 child task is blocked/);
    expect(s.actions[0]).toMatchObject({ kind: 'tab', tab: 'children' });
    expect(kinds(s)).toEqual(['tab', 'close']);
  });

  it('does not call an Issue finished before every child has loaded', () => {
    const issue = t({ kind: 'issue', status: 'in_progress', child_ticket_ids: ['a', 'b'] });
    const s = nextStep({ ticket: issue, children: [t({ id: 'a', status: 'done' })], perms: ALL, today: TODAY })!;
    expect(s.id).toBe('issue_progress');
  });

  it('points an Issue with no children at the child tasks tab', () => {
    const s = nextStep({ ticket: t({ kind: 'issue', status: 'in_progress' }), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('issue_empty');
    expect(kinds(s)).toEqual(['tab', 'close']);
  });

  it('explains a verified-clean review and names the CLI step', () => {
    const s = nextStep({ ticket: t({ status: 'review' }), review: review(['CHANGES_REQUESTED', 'VERIFIED-CLEAN']), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('review_clean');
    expect(s.title).toBe('Verified clean in round 2');
    expect(s.cli).toBe('sfs ticket complete tk_1');
    expect(s.actions).toEqual([{ kind: 'tab', tab: 'review', label: 'Open review', variant: 'secondary' }]);
  });

  it('counts open findings when changes were requested', () => {
    const s = nextStep({ ticket: t({ status: 'review' }), review: review(['CHANGES_REQUESTED'], 2), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('review_changes');
    expect(s.text).toBe('2 open findings for atlas to address.');
    expect(s.actions[0]).toMatchObject({ kind: 'tab', tab: 'review', variant: 'primary' });
  });

  it('waits for a reviewer when there is no trusted verdict', () => {
    const s = nextStep({ ticket: t({ status: 'review' }), review: null, perms: ALL, today: TODAY })!;
    expect(s.id).toBe('review_waiting');
    expect(s.actions[0]).toMatchObject({ kind: 'tab', tab: 'activity' });
  });

  it('names the dependency it waits on, with its status and due date', () => {
    const dep = t({ id: 'tk_dep', title: 'Rotate the key', status: 'in_progress', due_date: '2026-11-20' });
    const s = nextStep({ ticket: t({ status: 'blocked', depends_on: ['tk_dep'] }), dependencies: [dep], perms: ALL, today: TODAY })!;
    expect(s.id).toBe('waiting_on_dependency');
    expect(s.title).toBe('Waiting on tk_dep');
    expect(s.text).toBe('Rotate the key is in progress, due Nov 20.');
    expect(s.actions).toEqual([{ kind: 'goto', ticketId: 'tk_dep', label: 'Open blocker', variant: 'secondary' }]);
  });

  it('ignores finished dependencies and counts several open ones', () => {
    const deps = [t({ id: 'a', status: 'done' }), t({ id: 'b', status: 'open' }), t({ id: 'c', status: 'blocked' })];
    const ticket = t({ depends_on: ['a', 'b', 'c'] });
    expect(openBlockers(ticket, deps).map((d) => d.id)).toEqual(['b', 'c']);
    expect(nextStep({ ticket, dependencies: deps, perms: ALL, today: TODAY })!.title).toBe('Waiting on 2 tickets');
  });

  it('asks why a ticket is blocked without an open dependency', () => {
    const s = nextStep({ ticket: t({ status: 'blocked' }), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('blocked');
    expect(s.actions[0]).toMatchObject({ kind: 'tab', tab: 'activity' });
  });

  it('flags an overdue ticket with reassign and date actions the viewer may take', () => {
    const late = t({ status: 'in_progress', due_date: '2026-11-04' });
    const s = nextStep({ ticket: late, perms: ALL, today: TODAY })!;
    expect(s.id).toBe('overdue');
    expect(s.title).toBe('5 days overdue');
    expect(kinds(s)).toEqual(['assign', 'due']);
    expect(nextStep({ ticket: late, perms: { ...NONE, reassign: true }, today: TODAY })!.actions.map((a) => a.kind)).toEqual(['assign']);
    expect(nextStep({ ticket: t({ status: 'in_progress', due_date: '2026-11-08' }), perms: ALL, today: TODAY })!.title).toBe('1 day overdue');
  });

  it('asks for an owner when nobody has the ticket', () => {
    const s = nextStep({ ticket: t({ assigned_to: null }), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('unassigned');
    expect(kinds(s)).toEqual(['assign']);
    expect(nextStep({ ticket: t({ assigned_to: null }), perms: NONE, today: TODAY })!.actions).toEqual([]);
  });

  it('says progress comes from the agent while in progress', () => {
    const s = nextStep({ ticket: t({ status: 'in_progress', assigned_user_id: 'u2', assigned_to: null, assigned_user: { id: 'u2', display_name: 'Sam Rivera', email: 's@x' } }), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('in_progress');
    expect(s.title).toBe('Sam Rivera is working on this');
    expect(s.actions).toEqual([]);
  });

  it('says an open ticket is ready to start from the agent', () => {
    const s = nextStep({ ticket: t(), perms: ALL, today: TODAY })!;
    expect(s.id).toBe('ready');
    expect(s.cli).toBe('sfs ticket start tk_1');
    expect(s.actions).toEqual([]);
  });

  it('has nothing to say about a finished ticket', () => {
    for (const status of ['done', 'closed', 'cancelled']) {
      expect(nextStep({ ticket: t({ status, due_date: '2026-01-01', depends_on: ['x'] }), dependencies: [t({ id: 'x' })], perms: ALL, today: TODAY })).toBeNull();
    }
  });

  it('never offers a move the dashboard cannot make', () => {
    const statuses = ['suggested', 'open', 'in_progress', 'blocked', 'review', 'done', 'closed', 'cancelled'];
    for (const status of statuses) {
      for (const kind of ['task', 'issue']) {
        const s = nextStep({ ticket: t({ status, kind }), perms: ALL, today: TODAY });
        for (const a of s?.actions ?? []) {
          expect(['approve', 'dismiss', 'close', 'assign', 'due', 'tab', 'goto']).toContain(a.kind);
          if (a.kind === 'approve' || a.kind === 'dismiss') expect(status).toBe('suggested');
          if (a.kind === 'close') expect([kind, status]).toEqual(['issue', 'in_progress']);
        }
      }
    }
  });

  it('rolls up children by status', () => {
    expect(childRollup([t({ status: 'done' }), t({ status: 'blocked' }), t({ status: 'open' })])).toEqual({ done: 1, total: 3, blocked: 1 });
  });
});
