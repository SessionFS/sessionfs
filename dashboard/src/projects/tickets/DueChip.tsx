import type { Ticket } from '../../api/client';
import { formatDueDate, isOverdue } from './schedule';

/** Compact due date for list rows and board cards; nothing when unset.
 *  Overdue (past due, not done/closed/cancelled) reads in the danger tone. */
export function DueChip({ ticket, className = '' }: { ticket: Pick<Ticket, 'status' | 'due_date'>; className?: string }) {
  if (!ticket.due_date) return null;
  const overdue = isOverdue(ticket);
  const date = formatDueDate(ticket.due_date);
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap ${
        overdue ? 'font-medium text-danger' : 'text-text-tertiary'
      } ${className}`}
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="3" y="4" width="18" height="18" rx="2" />
        <line x1="16" y1="2" x2="16" y2="6" />
        <line x1="8" y1="2" x2="8" y2="6" />
        <line x1="3" y1="10" x2="21" y2="10" />
      </svg>
      {overdue ? `Overdue · ${date}` : `Due ${date}`}
    </span>
  );
}
