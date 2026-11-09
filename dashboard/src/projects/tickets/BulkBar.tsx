import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { Ticket } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { Button, Dialog, DialogFooter, DialogHeader, IconButton } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import { AssigneePicker } from './AssigneePicker';
import { ReapprovalConfirm } from './ReapprovalConfirm';
import { RowPopover } from './RowPopover';
import { UNASSIGNED, assigneePayload, sameAssignee, ticketAssignee, type Assignee } from './assignee';
import { BULK_CONCURRENCY, bulkSummary, bulkTargets, runBounded, type BulkResult } from './bulk';
import { useTicketViewer } from './viewer';

/* ── Bulk bar ──
 *
 * Floats at the bottom while rows are selected: Assign…, Approve (the
 * Suggested ones), Dismiss (Suggested and Open, after a confirm), Follow,
 * and Clear. A count on Approve / Dismiss says when they reach only part of
 * the selection; an action that reaches none says why. Each runs the
 * single-ticket call per ticket (four at a time), then reports one line;
 * tickets that failed stay selected. No status is changed optimistically:
 * the list refreshes from the server. */

type Verb = 'approve' | 'dismiss' | 'assign' | 'unassign' | 'follow';

function useBulkRunner(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  const { addToast } = useToast();
  return async function run(
    verb: Verb,
    tickets: Ticket[],
    call: (t: Ticket) => Promise<unknown>,
  ): Promise<BulkResult<Ticket>> {
    if (!auth) throw new Error('Not signed in');
    const result = await runBounded(tickets, BULK_CONCURRENCY, call);
    void qc.invalidateQueries({ queryKey: ['tickets', projectId] });
    for (const t of result.ok) {
      void qc.invalidateQueries({ queryKey: verb === 'follow' ? ['ticketWatchers', projectId, t.id] : ['ticket', projectId, t.id] });
    }
    const { tone, text } = bulkSummary(verb, result);
    addToast(tone, text);
    return result;
  };
}

export function BulkBar({
  projectId,
  selected,
  onSettled,
  onClear,
  orgId,
  isProjectAdmin,
  assignOpen,
  onAssignOpenChange,
}: {
  projectId: string;
  /** The selected tickets on screen, in list order. */
  selected: Ticket[];
  /** After an action: the tickets to keep selected (the ones that failed). */
  onSettled: (keepIds: string[]) => void;
  onClear: () => void;
  orgId?: string | null;
  isProjectAdmin?: boolean;
  /** The Assign… panel, open from here or from the A key. */
  assignOpen: boolean;
  onAssignOpenChange: (open: boolean) => void;
}) {
  const { auth } = useAuth();
  const viewer = useTicketViewer();
  const run = useBulkRunner(projectId);
  const [busy, setBusy] = useState<Verb | null>(null);
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const targets = bulkTargets(selected, viewer, isProjectAdmin);
  const n = selected.length;
  const client = auth?.client;

  async function act(verb: Verb, tickets: Ticket[], call: (t: Ticket) => Promise<unknown>) {
    setBusy(verb);
    try {
      const result = await run(verb, tickets, call);
      onSettled(result.failed.map((f) => f.item.id));
    } finally {
      setBusy(null);
    }
  }

  const partial = (k: number) => (k && k !== n ? ` ${k}` : '');

  return (
    <div className="tk-bulk" role="group" aria-label="Bulk actions">
      <span className="tk-bulk-n" aria-live="polite">
        {n} selected
      </span>
      <RowPopover
        kind="dialog"
        label={`Assign ${n} ${n === 1 ? 'ticket' : 'tickets'}`}
        side="top"
        open={assignOpen}
        onOpenChange={onAssignOpenChange}
        panelClassName="w-80"
        trigger={(props) => (
          <Button
            {...props}
            variant="ghost"
            size="sm"
            disabled={!targets.assign.length || busy !== null}
            disabledReason={targets.assignReason}
            loading={busy === 'assign' || busy === 'unassign'}
          >
            Assign…
          </Button>
        )}
      >
        {(close) => (
          <BulkAssignPanel
            projectId={projectId}
            orgId={orgId}
            tickets={targets.assign}
            skipped={n - targets.assign.length}
            pending={busy === 'assign' || busy === 'unassign'}
            onCancel={() => close(true)}
            onApply={(target, tickets) => {
              close(false);
              void act(target.kind === 'none' ? 'unassign' : 'assign', tickets, (t) =>
                client!.updateTicket(projectId, t.id, assigneePayload(target, { clearContact: !!t.assigned_contact_id })),
              );
            }}
          />
        )}
      </RowPopover>
      <Button
        variant="ghost"
        size="sm"
        disabled={!targets.approve.length || busy !== null}
        disabledReason={targets.approveReason}
        loading={busy === 'approve'}
        aria-label={partial(targets.approve.length) ? `Approve ${targets.approve.length} suggested` : undefined}
        onClick={() => void act('approve', targets.approve, (t) => client!.approveTicket(projectId, t.id))}
      >
        Approve{partial(targets.approve.length)}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="tk-bulk-danger"
        disabled={!targets.dismiss.length || busy !== null}
        disabledReason={targets.dismissReason}
        loading={busy === 'dismiss'}
        aria-label={partial(targets.dismiss.length) ? `Dismiss ${targets.dismiss.length} of ${n}` : undefined}
        onClick={() => setConfirmDismiss(true)}
      >
        Dismiss{partial(targets.dismiss.length)}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        disabled={busy !== null}
        loading={busy === 'follow'}
        onClick={() => void act('follow', selected, (t) => client!.watchTicket(projectId, t.id, false))}
      >
        Follow
      </Button>
      <span className="tk-bulk-div" aria-hidden="true" />
      <IconButton label="Clear selection (Esc)" size="sm" onClick={onClear} disabled={busy !== null}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </IconButton>

      <BulkDismissConfirm
        open={confirmDismiss}
        count={targets.dismiss.length}
        skipped={n - targets.dismiss.length}
        pending={busy === 'dismiss'}
        onCancel={() => setConfirmDismiss(false)}
        onConfirm={async () => {
          await act('dismiss', targets.dismiss, (t) => client!.dismissTicket(projectId, t.id));
          setConfirmDismiss(false);
        }}
      />
    </div>
  );
}

function BulkDismissConfirm({
  open,
  count,
  skipped,
  pending,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  count: number;
  skipped: number;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const titleId = 'tk-bulk-dismiss-title';
  const what = `${count} ${count === 1 ? 'ticket' : 'tickets'}`;
  return (
    <Dialog open={open} onClose={onCancel} titleId={titleId} className="max-w-md">
      <DialogHeader titleId={titleId}>Dismiss {what}?</DialogHeader>
      <p className="text-sm text-text-secondary">
        {count === 1 ? 'It moves' : 'They move'} to Cancelled. A cancelled ticket can’t be reopened.
        {skipped > 0 && ` ${skipped} other selected ${skipped === 1 ? 'ticket is' : 'tickets are'} left as ${skipped === 1 ? 'it is' : 'they are'}.`}
      </p>
      <DialogFooter>
        <Button variant="secondary" type="button" onClick={onCancel}>
          Keep them
        </Button>
        <Button variant="danger" type="button" loading={pending} onClick={() => void onConfirm()}>
          Dismiss {what}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

/** Pick one assignee for every selected ticket the viewer may reassign.
 *  Tickets that already have it are left alone; a non-editor's change to an
 *  approved ticket sends it back for approval, so that asks first. */
function BulkAssignPanel({
  projectId,
  orgId,
  tickets,
  skipped,
  pending,
  onCancel,
  onApply,
}: {
  projectId: string;
  orgId?: string | null;
  tickets: Ticket[];
  skipped: number;
  pending: boolean;
  onCancel: () => void;
  onApply: (target: Assignee, tickets: Ticket[]) => void;
}) {
  const viewer = useTicketViewer();
  const [value, setValue] = useState<Assignee>(UNASSIGNED);
  const [touched, setTouched] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const pickerId = 'tk-bulk-assign';
  useEffect(() => {
    document.getElementById(pickerId)?.focus();
  }, []);

  const changing = tickets.filter((t) => !sameAssignee(ticketAssignee(t), value));
  const sendsBack = !viewer.canEdit && changing.some((t) => t.status === 'open');

  function apply() {
    if (!changing.length) {
      onCancel();
      return;
    }
    if (sendsBack && !confirming) {
      setConfirming(true);
      return;
    }
    onApply(value, changing);
  }

  return (
    <div className="px-3 py-2 space-y-3">
      <AssigneePicker
        id={pickerId}
        projectId={projectId}
        orgId={orgId}
        value={value}
        onChange={(next) => {
          setValue(next);
          setTouched(true);
          setConfirming(false);
        }}
      />
      <p className="text-xs text-text-tertiary">
        {touched
          ? `Changes ${changing.length} of ${tickets.length} ${tickets.length === 1 ? 'ticket' : 'tickets'}.`
          : `Pick who takes ${tickets.length === 1 ? 'this ticket' : `these ${tickets.length} tickets`}.`}
        {skipped > 0 && ` ${skipped} you can’t reassign ${skipped === 1 ? 'is' : 'are'} left out.`}
      </p>
      {confirming ? (
        <ReapprovalConfirm onConfirm={() => onApply(value, changing)} onCancel={() => setConfirming(false)} pending={pending} />
      ) : (
        <div className="flex items-center gap-2">
          <Button variant="primary" size="sm" onClick={apply} disabled={!touched || !changing.length} loading={pending}>
            {value.kind === 'none' ? 'Unassign' : 'Assign'}
          </Button>
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
