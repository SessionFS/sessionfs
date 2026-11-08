import { useMemo, useState } from 'react';
import type { MeetingActionItem, MeetingItemTicketRef } from '../api/client';
import { Alert, Button, Combobox, Dialog, DialogFooter, DialogHeader, type ComboboxOption } from '../components/ui';
import { useLinkMeetingItem, useLinkPreview, useLinkableTickets } from '../hooks/useMeetings';
import { useToast } from '../hooks/useToast';
import { formatStatusLabel } from '../projects/tickets/constants';
import { messagesLabel } from './chat';
import { linesLabel, meetingActionError } from './format';

interface LinkTicketDialogProps {
  meetingId: string;
  item: MeetingActionItem;
  onClose: () => void;
  /** The item cites chat messages, not transcript lines. */
  chat?: boolean;
}

function ticketOption(t: MeetingItemTicketRef): ComboboxOption {
  return {
    value: t.id,
    label: t.title,
    description: `${t.id} · ${formatStatusLabel(t.status)}`,
    searchText: `${t.title} ${t.id}`,
  };
}

/**
 * Tie an action item to a ticket that already exists: search the tickets
 * this conversation can link to, see the comment Link will add, then Link.
 * While the link is written the dialog stays open and locked.
 */
export function LinkTicketDialog({ meetingId, item, onClose, chat = false }: LinkTicketDialogProps) {
  const [query, setQuery] = useState('');
  const [ticketId, setTicketId] = useState('');
  const [picked, setPicked] = useState<MeetingItemTicketRef | null>(null);
  const [error, setError] = useState<string | null>(null);
  const tickets = useLinkableTickets(meetingId, query);
  const preview = useLinkPreview(meetingId, item.id, ticketId || undefined);
  const link = useLinkMeetingItem(meetingId);
  const { addToast } = useToast();
  const linking = link.isPending;
  const range = { start: item.line_start, end: item.line_end };
  const where = chat ? messagesLabel(range) : linesLabel(range);

  const options = useMemo(() => (tickets.data?.tickets ?? []).map(ticketOption), [tickets.data]);

  const close = () => {
    if (!linking) onClose();
  };

  async function submit() {
    if (!ticketId || !preview.data) return;
    setError(null);
    try {
      const result = await link.mutateAsync({ itemId: item.id, ticketId });
      addToast('success', `Linked to ${result.ticket_id}`);
      onClose();
    } catch (err) {
      setError(meetingActionError(err));
    }
  }

  return (
    <Dialog open onClose={close} titleId="meeting-link-title" className="max-w-xl">
      <DialogHeader titleId="meeting-link-title">Link to a ticket</DialogHeader>
      <p className="-mt-2 mb-4 text-sm text-text-tertiary">
        Tie this item to a ticket that already exists instead of creating a new one. The ticket gets a comment citing{' '}
        {where.toLowerCase()}.
      </p>

      <fieldset disabled={linking} className="space-y-4">
        <Combobox
          title="Ticket"
          value={ticketId}
          onValueChange={(value) => {
            setTicketId(value);
            setError(null);
            setPicked(tickets.data?.tickets.find((t) => t.id === value) ?? null);
          }}
          options={options}
          selectedLabel={picked?.title}
          placeholder="Choose a ticket"
          searchPlaceholder="Search by title or ticket id"
          onSearchChange={setQuery}
          loading={tickets.searching && !tickets.isError}
          pending={tickets.searching && !tickets.isError}
          disabled={linking}
          emptyMessage={
            tickets.isError ? meetingActionError(tickets.error) : 'No tickets here yet. Create a new one instead.'
          }
          noMatchesMessage={(q) => (tickets.isError ? meetingActionError(tickets.error) : `No tickets match “${q}”`)}
        />

        {ticketId && preview.isLoading && (
          <div className="h-16 rounded-md bg-bg-elevated animate-pulse" aria-busy="true" aria-label="Loading the comment" />
        )}
        {preview.isError && <Alert variant="danger">{meetingActionError(preview.error)}</Alert>}
        {preview.data && (
          <div>
            <p className="mb-1.5 text-xs font-medium text-text-secondary">Adds this comment on {preview.data.ticket.id}</p>
            <p className="whitespace-pre-wrap break-words rounded-md border border-border bg-bg-sunken px-3 py-2 text-sm text-text-primary">
              {preview.data.comment}
            </p>
          </div>
        )}
        {error && <Alert variant="danger">{error}</Alert>}
      </fieldset>

      <DialogFooter>
        <Button variant="secondary" onClick={close} disabled={linking}>
          Cancel
        </Button>
        <Button onClick={() => void submit()} disabled={!preview.data || linking}>
          {linking ? 'Linking…' : 'Link'}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
