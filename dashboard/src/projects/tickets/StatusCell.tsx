import { useToast } from '../../hooks/useToast';
import { useApproveTicket, useCloseTicket, useDismissTicket } from '../../hooks/useTickets';
import { type Ticket } from '../../api/client';
import { Dropdown } from '../../components/ui';
import { Badge } from '../../components/Badge';
import { STATUS_DOT, STATUS_VARIANT, formatStatusLabel } from './constants';
import { errorMessage } from '../../utils/errorMessage';

/**
 * Per-row status cell — tint Badge with colored dot.
 * When the ticket has dashboard-legal transitions, a chevron opens
 * a Dropdown listing ONLY those actions (approve / dismiss / close).
 * CLI/MCP-only transitions (start/complete/resolve/escalate/block)
 * are never listed. When no legal transitions, renders a static label.
 */
export function StatusCell({ ticket, projectId }: { ticket: Ticket; projectId: string }) {
  const approve = useApproveTicket(projectId);
  const dismiss = useDismissTicket(projectId);
  const close = useCloseTicket(projectId);
  const { addToast } = useToast();

  const canApprove = ticket.status === 'suggested';
  const canDismiss = ticket.status === 'suggested' || ticket.status === 'open';
  const canClose = ticket.kind === 'issue' && ticket.status === 'in_progress';
  const hasActions = canApprove || canDismiss || canClose;

  const variant = STATUS_VARIANT[ticket.status] || 'default';
  const dotClass = STATUS_DOT[ticket.status] || 'bg-text-tertiary';
  const label = formatStatusLabel(ticket.status);

  async function handleAction(action: string) {
    try {
      if (action === 'approve') {
        await approve.mutateAsync(ticket.id);
        addToast('success', 'Approved');
      } else if (action === 'dismiss') {
        await dismiss.mutateAsync(ticket.id);
        addToast('success', 'Dismissed');
      } else if (action === 'close') {
        await close.mutateAsync(ticket.id);
        addToast('success', 'Issue closed');
      }
    } catch (exc) {
      addToast('error', `Update failed: ${errorMessage(exc)}`);
    }
  }

  const actionItems = [
    ...(canApprove ? [{ key: 'approve', label: 'Approve' }] : []),
    ...(canDismiss ? [{ key: 'dismiss', label: 'Dismiss' }] : []),
    ...(canClose ? [{ key: 'close', label: 'Close issue' }] : []),
  ];

  if (!hasActions) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`} />
        <Badge variant={variant} tint label={label} size="sm" />
      </span>
    );
  }

  return (
    <Dropdown
      trigger={() => (
        <button
          type="button"
          aria-label={`Status: ${label}. Change status`}
          className="inline-flex items-center gap-1.5 px-1 -mx-1 py-0.5 rounded hover:bg-surface-hover transition-colors"
        >
          <span aria-hidden="true" className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`} />
          <Badge variant={variant} tint label={label} size="sm" />
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-text-tertiary">
            <polyline points="2,3 5,6 8,3" />
          </svg>
        </button>
      )}
      items={actionItems}
      onSelect={handleAction}
      menuLabel="Ticket actions"
    />
  );
}
