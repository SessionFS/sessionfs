import { useState } from 'react';
import type { Ticket } from '../../api/client';
import { Button, Dialog, DialogFooter, DialogHeader } from '../../components/ui';
import { useApproveTicket, useCloseTicket, useDismissTicket } from '../../hooks/useTickets';
import { useToast } from '../../hooks/useToast';
import { errorMessage } from '../../utils/errorMessage';
import { formatStatusLabel } from './constants';
import { RowPopover } from './RowPopover';
import { StatusIcon } from './StatusIcon';
import { ticketActionAllowed, useTicketViewer } from './viewer';
import { dashboardMoves } from './views';

/* ── Status menu ──
 *
 * The row's status icon is a button. Its menu lists only the moves the
 * dashboard can make (approve, dismiss, close an Issue) that the viewer is
 * allowed (viewer.ts mirrors the server), and says where every other move
 * happens: start, complete, block and resolve run where the work runs. A
 * dismiss cancels the ticket, so it asks first. */

const MOVE_COPY: Record<'approve' | 'dismiss' | 'close', { label: string; hint: string; icon: string }> = {
  approve: { label: 'Approve', hint: 'to Open', icon: 'open' },
  dismiss: { label: 'Dismiss…', hint: 'to Cancelled', icon: 'cancelled' },
  close: { label: 'Close issue', hint: 'to Closed', icon: 'closed' },
};

export function StatusMenu({
  ticket,
  projectId,
  showLabel = false,
  tabIndex,
}: {
  ticket: Ticket;
  projectId: string;
  /** Roving tabindex from the row (-1 off the focused row). */
  tabIndex?: number;
  /** Show the status name beside the icon (the ticket pane's property row). */
  showLabel?: boolean;
}) {
  const approve = useApproveTicket(projectId);
  const dismiss = useDismissTicket(projectId);
  const close = useCloseTicket(projectId);
  const { addToast } = useToast();
  const viewer = useTicketViewer();
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const label = formatStatusLabel(ticket.status);
  const moves = dashboardMoves(ticket).filter((m) => ticketActionAllowed(m, ticket, viewer));

  async function run(move: 'approve' | 'dismiss' | 'close') {
    try {
      if (move === 'approve') {
        await approve.mutateAsync(ticket.id);
        addToast('success', 'Approved');
      } else if (move === 'dismiss') {
        await dismiss.mutateAsync(ticket.id);
        addToast('success', 'Dismissed');
      } else {
        await close.mutateAsync(ticket.id);
        addToast('success', 'Issue closed');
      }
    } catch (exc) {
      addToast('error', `Update failed: ${errorMessage(exc)}`);
    }
  }

  return (
    <>
      <RowPopover
        kind="menu"
        label={`Status actions for ${ticket.id}`}
        className="tk-stcell"
        panelClassName="w-72"
        trigger={(props) => (
          <button
            type="button"
            {...props}
            tabIndex={tabIndex}
            data-tk-status=""
            className={showLabel ? 'tk-stprop' : 'tk-stbtn'}
            aria-label={`Status: ${label}. Show status actions`}
            title={showLabel ? undefined : label}
          >
            <StatusIcon status={ticket.status} size={showLabel ? 14 : 16} />
            {showLabel && <span className="tk-stprop-label">{label}</span>}
          </button>
        )}
      >
        {(closeMenu) => (
          <>
            <div className="tk-pop-head">
              <StatusIcon status={ticket.status} size={12} />
              <span>{label}</span>
            </div>
            {moves.length > 0 ? (
              <div role="menu" aria-label={`Status actions for ${ticket.id}`} className="py-1">
                {moves.map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="menuitem"
                    className="tk-menuitem"
                    onClick={() => {
                      closeMenu(true);
                      if (m === 'dismiss') setConfirmDismiss(true);
                      else void run(m);
                    }}
                  >
                    <StatusIcon status={MOVE_COPY[m].icon} size={14} />
                    <span className="flex-1">{MOVE_COPY[m].label}</span>
                    <span className="tk-menuitem-hint">{MOVE_COPY[m].hint}</span>
                  </button>
                ))}
              </div>
            ) : (
              <p className="px-3 py-1.5 text-sm text-text-secondary">No status change from here.</p>
            )}
            <p className="tk-pop-note">
              Start, complete, block and resolve happen where the work runs: <code>sfs ticket start</code> or the
              agent’s MCP tools.
            </p>
          </>
        )}
      </RowPopover>
      <DismissConfirm
        ticket={ticket}
        open={confirmDismiss}
        pending={dismiss.isPending}
        onCancel={() => setConfirmDismiss(false)}
        onConfirm={async () => {
          await run('dismiss');
          setConfirmDismiss(false);
        }}
      />
    </>
  );
}

/** Dismissing cancels the ticket for good, so every dismiss asks first: the
 *  status menu and the ticket pane's next step both use this. */
export function DismissConfirm({
  ticket,
  open,
  pending,
  onCancel,
  onConfirm,
}: {
  ticket: Pick<Ticket, 'id' | 'title'>;
  open: boolean;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const titleId = `dismiss-${ticket.id}-title`;
  return (
    <Dialog open={open} onClose={onCancel} titleId={titleId} className="max-w-md">
      <DialogHeader titleId={titleId}>Dismiss this ticket?</DialogHeader>
      <p className="text-sm text-text-secondary">
        <span className="font-medium text-text-primary">{ticket.title}</span> moves to Cancelled. A cancelled ticket
        can’t be reopened.
      </p>
      <DialogFooter>
        <Button variant="secondary" type="button" onClick={onCancel}>
          Keep it
        </Button>
        <Button variant="danger" type="button" loading={pending} onClick={() => void onConfirm()}>
          Dismiss ticket
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
