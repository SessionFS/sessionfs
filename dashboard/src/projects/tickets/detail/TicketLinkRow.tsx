import type { ReactNode } from 'react';
import type { Ticket } from '../../../api/client';
import { AssigneeLabel } from '../AssigneeLabel';
import { formatStatusLabel } from '../constants';
import { formatDueDate, isOverdue } from '../schedule';
import { StatusIcon } from '../StatusIcon';

/** One related ticket (a dependency or a child task) as a single button:
 *  status, id, title, due date and owner, live from its own query. */
export function TicketLinkRow({
  id,
  ticket,
  failed,
  onOpen,
}: {
  id: string;
  ticket?: Ticket;
  failed?: boolean;
  onOpen: (id: string) => void;
}) {
  let body: ReactNode;
  if (ticket) {
    const late = isOverdue(ticket);
    body = (
      <>
        <StatusIcon status={ticket.status} size={14} />
        <span className="sr-only">{formatStatusLabel(ticket.status)}:</span>
        <span className="shrink-0 font-mono text-2xs text-text-tertiary">{id}</span>
        <span className="min-w-0 flex-1 truncate text-text-primary">{ticket.title}</span>
        {ticket.due_date && (
          <span className={`shrink-0 text-2xs tabular-nums ${late ? 'font-medium text-danger' : 'text-text-tertiary'}`}>
            {late ? 'Overdue' : 'Due'} {formatDueDate(ticket.due_date)}
          </span>
        )}
        {(ticket.assigned_to || ticket.assigned_user_id || ticket.assigned_contact_id) && (
          <span className="hidden min-w-0 max-w-[30%] shrink-0 text-2xs text-text-tertiary sm:inline-flex">
            <AssigneeLabel ticket={ticket} />
          </span>
        )}
      </>
    );
  } else {
    body = (
      <>
        <span className="shrink-0 font-mono text-2xs text-text-tertiary">{id}</span>
        <span className={`text-xs ${failed ? 'text-danger' : 'text-text-tertiary'}`}>
          {failed ? '(failed to load)' : 'loading…'}
        </span>
      </>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onOpen(id)}
      className="flex w-full min-w-0 items-center gap-2 rounded-md border border-border bg-surface px-2.5 py-2 text-left text-sm hover:bg-surface-hover outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
    >
      {body}
    </button>
  );
}
