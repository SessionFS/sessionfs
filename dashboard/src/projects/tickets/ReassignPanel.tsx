import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import type { Ticket } from '../../api/client';
import { Button } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import { useUpdateTicket } from '../../hooks/useTickets';
import { errorMessage } from '../../utils/errorMessage';
import { AssigneePicker } from './AssigneePicker';
import { ReapprovalConfirm } from './ReapprovalConfirm';
import { UNASSIGNED, assigneeErrorFrom, assigneePayload, sameAssignee, ticketAssignee, type Assignee } from './assignee';

/* ── Reassign ──
 *
 * Inline editor under the ticket header. The choice is staged so the
 * notification note can be read before saving; Save sends the whole
 * assignee (the chosen field plus an explicit null for the other).
 * Unassign clears both fields in one click. A non-editor's change to an
 * approved ticket sends it back for approval, so those saves ask first. */

export function ReassignPanel({
  projectId,
  ticket,
  onDone,
  needsReapproval = false,
  orgId,
}: {
  projectId: string;
  ticket: Ticket;
  onDone: () => void;
  /** The project's org when the viewer is a member: offers its contacts. */
  orgId?: string | null;
  /** Saving withdraws the ticket's approval (non-editor, status open). */
  needsReapproval?: boolean;
}) {
  const current = ticketAssignee(ticket);
  const [value, setValue] = useState<Assignee>(current);
  const [error, setError] = useState<string | undefined>();
  const update = useUpdateTicket(projectId);
  const { addToast } = useToast();
  const unchanged = sameAssignee(value, current);
  const pickerId = `reassign-${ticket.id}`;

  const [confirming, setConfirming] = useState(false);

  // Opening the editor moves focus to the picker.
  useEffect(() => {
    document.getElementById(pickerId)?.focus();
  }, [pickerId]);
  // Leaving the confirm hands focus back to the picker.
  const wasConfirming = useRef(false);
  useEffect(() => {
    if (wasConfirming.current && !confirming) document.getElementById(pickerId)?.focus();
    wasConfirming.current = confirming;
  }, [confirming, pickerId]);

  const hasAssignee = current.kind !== 'none';

  function save(target: Assignee = value) {
    if (sameAssignee(target, current)) {
      onDone();
      return;
    }
    if (needsReapproval && !confirming) {
      setConfirming(true);
      return;
    }
    void commit(target);
  }

  // One click: stage "Unassigned" (so the picker shows what is being saved)
  // and save it, through the re-approval confirm when that applies.
  function unassign() {
    setValue(UNASSIGNED);
    setError(undefined);
    save(UNASSIGNED);
  }

  // Escape while confirming backs out of the confirm only, and never
  // reaches a surrounding drawer.
  function handleKeyDown(e: KeyboardEvent) {
    if (!confirming || e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    if (!update.isPending) setConfirming(false);
  }

  async function commit(target: Assignee = value) {
    setError(undefined);
    try {
      await update.mutateAsync({ ticketId: ticket.id, body: assigneePayload(target, { clearContact: !!ticket.assigned_contact_id }) });
      addToast('success', target.kind === 'none' ? 'Unassigned' : 'Assignee updated');
      onDone();
    } catch (exc) {
      setConfirming(false);
      const problem = assigneeErrorFrom(exc);
      if (problem) setError(problem);
      addToast('error', `Reassign failed: ${problem ?? errorMessage(exc)}`);
    }
  }

  return (
    <div
      className="rounded-lg border border-border bg-surface p-3"
      role="group"
      aria-label="Reassign ticket"
      onKeyDown={handleKeyDown}
    >
      <div className="max-w-sm">
        <AssigneePicker
          id={pickerId}
          projectId={projectId}
          value={value}
          onChange={(next) => {
            setValue(next);
            setError(undefined);
          }}
          currentUser={ticket.assigned_user ?? null}
          currentContact={ticket.assigned_contact ?? null}
          orgId={orgId}
          error={error}
        />
      </div>
      {confirming ? (
        <ReapprovalConfirm onConfirm={() => void commit()} onCancel={() => setConfirming(false)} pending={update.isPending} />
      ) : (
        <div className="mt-3 flex items-center gap-2">
          <Button variant="primary" size="sm" onClick={() => save()} loading={update.isPending} disabled={unchanged}>
            Save
          </Button>
          <Button variant="ghost" size="sm" onClick={onDone} disabled={update.isPending}>
            Cancel
          </Button>
          {hasAssignee && (
            <Button variant="ghost" size="sm" className="ml-auto" onClick={unassign} disabled={update.isPending}>
              Unassign
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
