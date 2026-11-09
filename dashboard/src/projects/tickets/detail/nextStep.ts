import type { Ticket, TicketReviewState } from '../../../api/client';
import { formatStatusLabel } from '../constants';
import { ticketPersonName } from '../assignee';
import { TERMINAL_STATUSES, formatDueDate, todayIso } from '../schedule';

/* ── What should happen next ──
 *
 * One pure function decides the card at the top of the ticket pane: what
 * state the ticket is in, in a sentence, and the actions that move it on.
 * The only state changes it ever offers are the three the dashboard is
 * allowed to make (approve, dismiss, close an Issue). Everything else
 * (start, complete, resolve, block) runs where the work runs, so the card
 * names the CLI command instead of offering a button. */

export type PaneTab = 'overview' | 'activity' | 'review' | 'children' | 'jira';

export const PANE_TABS: PaneTab[] = ['overview', 'activity', 'review', 'children', 'jira'];

export function isPaneTab(v: string | null | undefined): v is PaneTab {
  return !!v && (PANE_TABS as string[]).includes(v);
}

export type NextStepAction =
  | { kind: 'approve' | 'dismiss' | 'close' | 'assign' | 'due'; label: string; variant: ActionVariant }
  | { kind: 'tab'; tab: PaneTab; label: string; variant: ActionVariant }
  | { kind: 'goto'; ticketId: string; label: string; variant: ActionVariant };

export type ActionVariant = 'primary' | 'secondary' | 'ghost' | 'danger';

export type NextStepTone = 'warning' | 'success' | 'brand' | 'danger' | 'info';

export interface NextStep {
  /** A stable name for the case, for tests and analytics-free debugging. */
  id:
    | 'needs_approval'
    | 'children_finished'
    | 'issue_progress'
    | 'issue_empty'
    | 'review_clean'
    | 'review_changes'
    | 'review_waiting'
    | 'waiting_on_dependency'
    | 'blocked'
    | 'overdue'
    | 'unassigned'
    | 'in_progress'
    | 'ready';
  tone: NextStepTone;
  title: string;
  /** Plain text; `{{glossary-id|words}}` marks a product term the card
   *  shows with its glossary definition. */
  text: string;
  /** A command that does the step from the agent's machine. */
  cli?: string;
  actions: NextStepAction[];
}

/** What the viewer may do from the dashboard (see tickets/viewer.ts). */
export interface NextStepPermissions {
  approve: boolean;
  dismiss: boolean;
  close: boolean;
  reassign: boolean;
  editSchedule: boolean;
}

export interface NextStepInput {
  ticket: Ticket;
  review?: TicketReviewState['review_state'] | null;
  /** The Issue's child tasks that have loaded. */
  children?: Ticket[];
  /** Tickets this one depends on that have loaded (finished ones included). */
  dependencies?: Ticket[];
  perms: NextStepPermissions;
  /** "YYYY-MM-DD", for tests. Defaults to today (UTC, the server's day). */
  today?: string;
}

const FINISHED = TERMINAL_STATUSES;

/** "atlas", "Sam Rivera", a contact's name; null when nobody. */
export function assigneeName(t: Ticket): string | null {
  if (t.assigned_user_id) return ticketPersonName(t);
  if (t.assigned_to) return t.assigned_to;
  if (t.assigned_contact) return t.assigned_contact.display_name;
  return null;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Whole days from `a` to `b`, both "YYYY-MM-DD". */
function daysBetween(a: string, b: string): number {
  const pa = Date.UTC(+a.slice(0, 4), +a.slice(5, 7) - 1, +a.slice(8, 10));
  const pb = Date.UTC(+b.slice(0, 4), +b.slice(5, 7) - 1, +b.slice(8, 10));
  return Math.round((pb - pa) / 86_400_000);
}

/** Dependencies that are not finished yet. */
export function openBlockers(t: Ticket, dependencies: Ticket[]): Ticket[] {
  const ids = new Set(t.depends_on);
  return dependencies.filter((d) => ids.has(d.id) && !FINISHED.has(d.status));
}

/** Child-task counts for an Issue, by the loaded children. */
export function childRollup(children: Ticket[]): { done: number; total: number; blocked: number } {
  return {
    done: children.filter((c) => FINISHED.has(c.status)).length,
    total: children.length,
    blocked: children.filter((c) => c.status === 'blocked').length,
  };
}

export function nextStep({
  ticket: t,
  review,
  children = [],
  dependencies = [],
  perms,
  today = todayIso(),
}: NextStepInput): NextStep | null {
  const who = assigneeName(t);
  const finished = FINISHED.has(t.status);

  if (t.status === 'suggested') {
    const by = t.created_by?.display_name?.trim() || t.created_by?.persona?.trim() || t.created_by_persona?.trim();
    const actions: NextStepAction[] = [];
    if (perms.approve) actions.push({ kind: 'approve', label: 'Approve', variant: 'primary' });
    if (perms.dismiss) actions.push({ kind: 'dismiss', label: 'Dismiss…', variant: 'danger' });
    return {
      id: 'needs_approval',
      tone: 'warning',
      title: 'Needs approval',
      text: `${by ? `${by} filed this as {{ticket-suggested|suggested}}. ` : 'This ticket is {{ticket-suggested|suggested}}. '}Approving opens it for work; dismissing cancels it.${
        actions.length === 0 ? ' A project editor makes that call.' : ''
      }`,
      actions,
    };
  }

  if (t.kind === 'issue' && t.status === 'in_progress') {
    // Before every child has loaded the counts would be wrong, so they
    // come from the ids the Issue lists.
    const total = t.child_ticket_ids.length;
    const roll = childRollup(children);
    if (total === 0) {
      return {
        id: 'issue_empty',
        tone: 'info',
        title: 'No child tasks yet',
        text: 'Add Tasks under this Issue to track the work. It closes once they are finished.',
        actions: [
          { kind: 'tab', tab: 'children', label: 'Child tasks', variant: 'secondary' },
          ...(perms.close ? [{ kind: 'close', label: 'Close issue', variant: 'ghost' } as const] : []),
        ],
      };
    }
    if (roll.total === total && roll.done === total) {
      return {
        id: 'children_finished',
        tone: 'success',
        title: 'All child tasks are finished',
        text: 'Close the Issue to roll it up.',
        actions: perms.close ? [{ kind: 'close', label: 'Close issue', variant: 'primary' }] : [],
      };
    }
    const actions: NextStepAction[] = [{ kind: 'tab', tab: 'children', label: 'See child tasks', variant: 'secondary' }];
    if (perms.close) actions.push({ kind: 'close', label: 'Close issue', variant: 'ghost' });
    return {
      id: 'issue_progress',
      tone: 'brand',
      title: `${roll.done} of ${total} child tasks finished`,
      text: `It closes when the rest are done.${roll.blocked ? ` ${plural(roll.blocked, 'child task is', 'child tasks are')} blocked.` : ''}`,
      actions,
    };
  }

  if (t.status === 'review') {
    const rounds = review?.rounds ?? [];
    const last = rounds[rounds.length - 1];
    if (last && last.verdict === 'VERIFIED-CLEAN') {
      return {
        id: 'review_clean',
        tone: 'success',
        title: `Verified clean in round ${last.round}`,
        text: 'A {{trusted-reviewer|trusted reviewer}} found no open findings. The implementer marks it done from the CLI once it merges.',
        cli: `sfs ticket complete ${t.id}`,
        actions: [{ kind: 'tab', tab: 'review', label: 'Open review', variant: 'secondary' }],
      };
    }
    if (last) {
      const open = review?.open_findings.length ?? 0;
      return {
        id: 'review_changes',
        tone: 'warning',
        title: `Changes requested in round ${last.round}`,
        text: `${plural(open, 'open finding')}${who ? ` for ${who} to address` : ' to address'}.`,
        actions: [{ kind: 'tab', tab: 'review', label: 'See findings', variant: 'primary' }],
      };
    }
    return {
      id: 'review_waiting',
      tone: 'info',
      title: 'Waiting for a reviewer',
      text: 'No verdict yet. Only verdicts from {{trusted-reviewer|trusted reviewers}} count toward the review.',
      actions: [{ kind: 'tab', tab: 'activity', label: 'Comment', variant: 'secondary' }],
    };
  }

  const blockers = finished ? [] : openBlockers(t, dependencies);
  if (blockers.length > 0) {
    const first = blockers[0];
    return {
      id: 'waiting_on_dependency',
      tone: 'danger',
      title: blockers.length === 1 ? `Waiting on ${first.id}` : `Waiting on ${blockers.length} tickets`,
      text: blockers
        .map(
          (b) =>
            `${b.title} is ${formatStatusLabel(b.status).toLowerCase()}${
              b.due_date ? `, due ${formatDueDate(b.due_date)}` : ''
            }.`,
        )
        .join(' '),
      actions: [{ kind: 'goto', ticketId: first.id, label: 'Open blocker', variant: 'secondary' }],
    };
  }
  if (t.status === 'blocked') {
    return {
      id: 'blocked',
      tone: 'danger',
      title: 'Blocked',
      text: 'Blocked without an open dependency. Ask in the activity what it is waiting for.',
      actions: [{ kind: 'tab', tab: 'activity', label: 'Ask why', variant: 'secondary' }],
    };
  }

  if (!finished && t.due_date && t.due_date < today) {
    const late = daysBetween(t.due_date, today);
    const actions: NextStepAction[] = [];
    if (perms.reassign) actions.push({ kind: 'assign', label: who ? 'Reassign' : 'Assign', variant: 'secondary' });
    if (perms.editSchedule) actions.push({ kind: 'due', label: 'Change due date', variant: 'ghost' });
    return {
      id: 'overdue',
      tone: 'danger',
      title: `${plural(late, 'day')} overdue`,
      text: `${who ? `Assigned to ${who}. ` : 'Nobody owns it. '}${
        actions.length ? 'Reassign it or move the date.' : 'Ask in the activity whether the date still holds.'
      }`,
      actions,
    };
  }

  if (!finished && !who) {
    return {
      id: 'unassigned',
      tone: 'info',
      title: 'Nobody owns this',
      text: perms.reassign ? 'Assign it to a person or an agent.' : 'A project editor can assign it to a person or an agent.',
      actions: perms.reassign ? [{ kind: 'assign', label: 'Assign', variant: 'primary' }] : [],
    };
  }

  if (t.status === 'in_progress') {
    return {
      id: 'in_progress',
      tone: 'brand',
      title: `${who} is working on this`,
      text: 'The agent holds its {{ticket-lease|lease}}: complete and resolve run from the CLI or through MCP. You can comment or reassign here.',
      actions: [],
    };
  }

  if (t.status === 'open') {
    return {
      id: 'ready',
      tone: 'info',
      title: `Ready for ${who} to start`,
      text: 'Work starts from the agent, with the CLI or the start_ticket tool.',
      cli: `sfs ticket start ${t.id}`,
      actions: [],
    };
  }

  return null;
}
