import { useState } from 'react';
import { Button, Dialog, DialogFooter, DialogHeader, Input } from '../components/ui';
import { useToast } from '../hooks/useToast';
import { useCreateReminder } from '../hooks/useReminders';
import { errorMessage } from '../utils/errorMessage';
import { fromLocalInput, userTimezone } from './format';

export function AddReminderDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} titleId="add-reminder-title">
      {open && <AddReminderForm onClose={onClose} />}
    </Dialog>
  );
}

function AddReminderForm({ onClose }: { onClose: () => void }) {
  const tz = userTimezone();
  const create = useCreateReminder();
  const { addToast } = useToast();
  const [title, setTitle] = useState('');
  const [when, setWhen] = useState('');
  const [error, setError] = useState<string | null>(null);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const due = fromLocalInput(when);
    if (!title.trim()) return setError('Give the reminder a title.');
    if (!due) return setError('Pick a date and time.');
    setError(null);
    create.mutate(
      { title: title.trim(), due_at: due, timezone: tz },
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
    <form onSubmit={submit} className="space-y-4">
      <DialogHeader titleId="add-reminder-title">Add reminder</DialogHeader>
      <Input
        title="Title"
        value={title}
        maxLength={200}
        autoFocus
        onChange={(e) => setTitle(e.target.value)}
      />
      <Input
        title="Due"
        type="datetime-local"
        value={when}
        onChange={(e) => setWhen(e.target.value)}
        hint={`Your time zone: ${tz}`}
      />
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" loading={create.isPending}>
          Add reminder
        </Button>
      </DialogFooter>
    </form>
  );
}
