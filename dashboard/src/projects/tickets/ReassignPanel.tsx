import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import type { Ticket } from '../../api/client';
import { Button } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import { useUpdateTicket } from '../../hooks/useTickets';
import { errorMessage } from '../../utils/errorMessage';
import { AssigneePicker } from './AssigneePicker';
import { ReapprovalConfirm } from './ReapprovalConfirm';
import { assigneeErrorFrom, assigneePayload, sameAssignee, ticketAssignee, type Assignee } from './assignee';

/* ── Reassign ──
 *
 * Inline editor under the ticket header. The choice is staged so the
 * notification note can be read before saving; Save sends the whole
 * assignee (the chosen field plus an explicit null for the other). A
 * non-editor's change to an approved ticket sends it back for approval, so
 * that Save asks first. */

export function ReassignPanel({
  projectId,
  ticket,
  onDone,
  needsReapproval = false,
}: {
  projectId: string;
  ticket: Ticket;
  onDone: () => void;
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

  function save() {
    if (unchanged) {
      onDone();
      return;
    }
    if (needsReapproval && !confirming) {
      setConfirming(true);
      return;
    }
    void commit();
  }

  // Escape while confirming backs out of the confirm only, and never
  // reaches a surrounding drawer.
  function handleKeyDown(e: KeyboardEvent) {
    if (!confirming || e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    if (!update.isPending) setConfirming(false);
  }

  async function commit() {
    setError(undefined);
    try {
      await update.mutateAsync({ ticketId: ticket.id, body: assigneePayload(value) });
      addToast('success', value.kind === 'none' ? 'Unassigned' : 'Assignee updated');
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
          error={error}
        />
      </div>
      {confirming ? (
        <ReapprovalConfirm onConfirm={() => void commit()} onCancel={() => setConfirming(false)} pending={update.isPending} />
      ) : (
        <div className="mt-3 flex items-center gap-2">
          <Button variant="primary" size="sm" onClick={save} loading={update.isPending} disabled={unchanged}>
            Save
          </Button>
          <Button variant="ghost" size="sm" onClick={onDone} disabled={update.isPending}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
