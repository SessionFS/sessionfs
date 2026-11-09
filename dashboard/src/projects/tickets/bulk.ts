import type { Ticket } from '../../api/client';
import { errorMessage } from '../../utils/errorMessage';
import { canReassignTicket, ticketActionAllowed, type TicketViewer } from './viewer';

/* ── Bulk actions ──
 *
 * The server has no bulk endpoint, so a bulk action is the single-ticket
 * call per ticket, at most four at a time. Each action only reaches the
 * tickets it applies to (Approve: Suggested; Dismiss: Suggested or Open;
 * both only where the viewer may make that move, the same rule as the
 * status menu). The result is summarised in one line, and the tickets that
 * failed stay selected for another try. */

export const BULK_CONCURRENCY = 4;

export interface BulkResult<T> {
  ok: T[];
  failed: { item: T; error: unknown }[];
}

/** Run `fn` over `items` with at most `limit` calls in flight. Never
 *  rejects: each failure is collected with its item. */
export async function runBounded<T>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<unknown>,
): Promise<BulkResult<T>> {
  const ok: T[] = [];
  const failed: { item: T; error: unknown }[] = [];
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const item = items[next++];
      try {
        await fn(item);
        ok.push(item);
      } catch (error) {
        failed.push({ item, error });
      }
    }
  }
  const n = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: n }, worker));
  // Report in the order given, not the order the calls finished.
  const order = new Map(items.map((t, i) => [t, i]));
  ok.sort((a, b) => order.get(a)! - order.get(b)!);
  failed.sort((a, b) => order.get(a.item)! - order.get(b.item)!);
  return { ok, failed };
}

export interface BulkTargets {
  approve: Ticket[];
  dismiss: Ticket[];
  assign: Ticket[];
  /** Why an action reaches none of the selection (for disabledReason). */
  approveReason?: string;
  dismissReason?: string;
  assignReason?: string;
}

/** Which selected tickets each bulk action applies to. */
export function bulkTargets(selected: readonly Ticket[], viewer: TicketViewer, isProjectAdmin = false): BulkTargets {
  const suggested = selected.filter((t) => t.status === 'suggested');
  const dismissable = selected.filter((t) => t.status === 'suggested' || t.status === 'open');
  const approve = suggested.filter((t) => ticketActionAllowed('approve', t, viewer));
  const dismiss = dismissable.filter((t) => ticketActionAllowed('dismiss', t, viewer));
  const assign = selected.filter((t) => canReassignTicket(t, viewer, isProjectAdmin));
  return {
    approve,
    dismiss,
    assign,
    approveReason: approve.length
      ? undefined
      : suggested.length
        ? 'Only project editors can approve tickets'
        : 'Only Suggested tickets can be approved',
    dismissReason: dismiss.length
      ? undefined
      : dismissable.length
        ? 'You can dismiss only tickets you created'
        : 'Only Suggested or Open tickets can be dismissed',
    assignReason: assign.length ? undefined : 'You can’t reassign these tickets',
  };
}

const PAST: Record<'approve' | 'dismiss' | 'assign' | 'unassign' | 'follow', [string, string]> = {
  approve: ['Approved', 'approve'],
  dismiss: ['Dismissed', 'dismiss'],
  assign: ['Assigned', 'assign'],
  unassign: ['Unassigned', 'unassign'],
  follow: ['Following', 'follow'],
};

/** One line for the toast: "Approved 5", "Approved 5 · 1 failed: <reason>",
 *  or "Couldn’t approve 2 tickets: <reason>". */
export function bulkSummary(
  verb: keyof typeof PAST,
  result: BulkResult<Pick<Ticket, 'id'>>,
): { tone: 'success' | 'error'; text: string } {
  const [done, infinitive] = PAST[verb];
  const n = result.ok.length;
  const f = result.failed.length;
  const count = (k: number) => `${k} ${k === 1 ? 'ticket' : 'tickets'}`;
  if (!f) return { tone: 'success', text: `${done} ${count(n)}` };
  const reason = errorMessage(result.failed[0].error);
  const more = f > 1 ? ` (and ${f - 1} more)` : '';
  if (!n) return { tone: 'error', text: `Couldn’t ${infinitive} ${count(f)}: ${reason}${more}` };
  return { tone: 'error', text: `${done} ${n} · ${f} failed: ${reason}${more}` };
}
