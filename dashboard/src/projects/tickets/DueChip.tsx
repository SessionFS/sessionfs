import type { Ticket } from '../../api/client';
import { formatDueDate, isOverdue } from './schedule';
import { Icon } from '../../components/ui/Icon';
import { Calendar } from 'lucide-react';

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
      <Icon icon={Calendar} size={14} />
      {overdue ? `Overdue · ${date}` : `Due ${date}`}
    </span>
  );
}
