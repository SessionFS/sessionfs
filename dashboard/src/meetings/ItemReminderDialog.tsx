import { useState } from 'react';
import type { MeetingActionItem, MeetingScope } from '../api/client';
import { Button, Dialog, DialogFooter, DialogHeader, Input } from '../components/ui';
import { useCreateMeetingItemReminder } from '../hooks/useReminders';
import { useToast } from '../hooks/useToast';
import { fromLocalInput, userTimezone } from '../reminders/format';
import { errorMessage } from '../utils/errorMessage';
import { messagesLabel } from './chat';
import { linesLabel } from './format';

interface Props {
  scope: MeetingScope;
  meetingId: string;
  item: MeetingActionItem;
  /** A chat thread's item cites messages, not lines. */
  chat?: boolean;
  onClose: () => void;
}

/** The suggested due date at 09:00 on the viewer's own clock. */
function initialWhen(item: MeetingActionItem): string {
  const s = item.suggested_reminder;
  if (!s) return '';
  return `${s.due_date}T09:00`;
}

/**
 * Add a reminder for one meeting item: pre-filled from the server's
 * suggestion (title, the due date at 09:00 in the viewer's time zone) and
 * editable. The viewer's IANA zone is sent so the server doesn't assume UTC.
 */
export function ItemReminderDialog({ scope, meetingId, item, chat = false, onClose }: Props) {
  const tz = userTimezone();
  const create = useCreateMeetingItemReminder(scope, meetingId);
  const { addToast } = useToast();
  const suggestion = item.suggested_reminder;
  const [title, setTitle] = useState(suggestion?.title ?? item.description.replace(/\s+/g, ' ').trim().slice(0, 200));
  const [when, setWhen] = useState(initialWhen(item));
  const [error, setError] = useState<string | null>(null);
  const range = { start: item.line_start, end: item.line_end };

  function close() {
    if (!create.isPending) onClose();
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const due = fromLocalInput(when);
    if (!title.trim()) return setError('Give the reminder a title.');
    if (!due) return setError('Pick a date and time.');
    setError(null);
    create.mutate(
      { itemId: item.id, body: { title: title.trim(), due_at: due, timezone: tz } },
      {
        onSuccess: () => {
          addToast('success', 'Reminder added');
          onClose();
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  }

  return (
    <Dialog open onClose={close} titleId="item-reminder-title">
      <form onSubmit={submit} className="space-y-4">
        <DialogHeader titleId="item-reminder-title">Add reminder</DialogHeader>
        <p className="-mt-2 text-sm text-text-tertiary">
          From {item.kind === 'decision' ? 'a decision' : 'an action item'}, {(chat ? messagesLabel(range) : linesLabel(range)).toLowerCase()}.
        </p>
        <Input title="Title" value={title} maxLength={200} autoFocus onChange={(e) => setTitle(e.target.value)} />
        <Input
          title="Due"
          type="datetime-local"
          value={when}
          onChange={(e) => setWhen(e.target.value)}
          hint={
            suggestion
              ? `From the item’s due date. Your time zone: ${tz}`
              : `This item has no due date; pick one. Your time zone: ${tz}`
          }
        />
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" loading={create.isPending}>
            Add reminder
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
