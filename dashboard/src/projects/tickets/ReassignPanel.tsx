import { useEffect, useState } from 'react';
import type { Ticket } from '../../api/client';
import { Button } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import { useUpdateTicket } from '../../hooks/useTickets';
import { errorMessage } from '../../utils/errorMessage';
import { AssigneePicker } from './AssigneePicker';
import { assigneeErrorFrom, assigneePayload, sameAssignee, ticketAssignee, type Assignee } from './assignee';

/* ── Reassign ──
 *
 * Inline editor under the ticket header. The choice is staged so the
 * notification note can be read before saving; Save sends the whole
 * assignee (the chosen field plus an explicit null for the other). */

export function ReassignPanel({
  projectId,
  ticket,
  onDone,
}: {
  projectId: string;
  ticket: Ticket;
  onDone: () => void;
}) {
  const current = ticketAssignee(ticket);
  const [value, setValue] = useState<Assignee>(current);
  const [error, setError] = useState<string | undefined>();
  const update = useUpdateTicket(projectId);
  const { addToast } = useToast();
  const unchanged = sameAssignee(value, current);
  const pickerId = `reassign-${ticket.id}`;

  // Opening the editor moves focus to the picker.
  useEffect(() => {
    document.getElementById(pickerId)?.focus();
  }, [pickerId]);

  async function save() {
    if (unchanged) {
      onDone();
      return;
    }
    setError(undefined);
    try {
      await update.mutateAsync({ ticketId: ticket.id, body: assigneePayload(value) });
      addToast('success', value.kind === 'none' ? 'Unassigned' : 'Assignee updated');
      onDone();
    } catch (exc) {
      const problem = assigneeErrorFrom(exc);
      if (problem) setError(problem);
      addToast('error', `Reassign failed: ${problem ?? errorMessage(exc)}`);
    }
  }

  return (
    <div className="rounded-lg border border-border bg-surface p-3" role="group" aria-label="Reassign ticket">
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
      <div className="mt-3 flex items-center gap-2">
        <Button variant="primary" size="sm" onClick={save} loading={update.isPending} disabled={unchanged}>
          Save
        </Button>
        <Button variant="ghost" size="sm" onClick={onDone} disabled={update.isPending}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
