import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Reminder } from '../api/client';
import { Badge } from '../components/Badge';
import RelativeDate from '../components/RelativeDate';
import {
  Button,
  Dialog,
  DialogFooter,
  DialogHeader,
  Drawer,
  Dropdown,
  Input,
  Textarea,
} from '../components/ui';
import {
  useAddReminderNote,
  useDeleteReminder,
  usePatchReminder,
  useReminder,
  useReminderNotes,
  useSnoozeReminder,
} from '../hooks/useReminders';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { formatDue, fromLocalInput, sourceHref, toLocalInput, userTimezone } from './format';

/** Drawer for one reminder, opened by `?reminder=<id>` on the Reminders page. */
export function ReminderDrawer({ reminderId, onClose }: { reminderId: string | null; onClose: () => void }) {
  const { data, isLoading, error } = useReminder(reminderId);
  return (
    <Drawer open={!!reminderId} onClose={onClose} titleId="reminder-drawer-title">
      {isLoading && <p className="text-sm text-text-tertiary">Loading reminder…</p>}
      {error && (
        <div>
          <h2 id="reminder-drawer-title" className="text-lg font-semibold text-text-primary">
            Reminder
          </h2>
          <p role="alert" className="mt-3 text-sm text-danger">
            {errorMessage(error, 'This reminder is not available.')}
          </p>
        </div>
      )}
      {data && <ReminderBody key={data.id} reminder={data} onClosed={onClose} />}
    </Drawer>
  );
}

type Modal = 'reschedule' | 'snooze' | 'dismiss' | 'delete' | 'rename' | null;

function ReminderBody({ reminder: r, onClosed }: { reminder: Reminder; onClosed: () => void }) {
  const { addToast } = useToast();
  const patch = usePatchReminder(r.id);
  const snooze = useSnoozeReminder(r.id);
  const del = useDeleteReminder(r.id);
  const [modal, setModal] = useState<Modal>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const open = r.status === 'open';
  const canClose = open && (r.is_owner || r.is_creator);
  const href = r.source && !r.source_hidden ? sourceHref(r.source) : null;

  function fail(err: unknown) {
    setActionError(errorMessage(err));
  }
  function run<T>(m: { mutate: (v: T, o: { onSuccess?: () => void; onError?: (e: unknown) => void }) => void }, v: T, ok: string, after?: () => void) {
    setActionError(null);
    m.mutate(v, {
      onSuccess: () => {
        addToast('success', ok);
        setModal(null);
        after?.();
      },
      onError: fail,
    });
  }

  return (
    <div className="space-y-5">
      <div className="pr-8">
        <h2 id="reminder-drawer-title" className="text-lg font-semibold text-text-primary [overflow-wrap:anywhere]">
          {r.title}
        </h2>
        <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-text-secondary">
          <span>{formatDue(r.due_at, r.timezone)}</span>
          {open && r.is_due && <Badge variant="warning" tint label="Due" size="sm" />}
          {!open && <Badge variant="default" tint label={r.status === 'done' ? 'Done' : 'Dismissed'} size="sm" />}
        </p>
        {r.snooze_count > 0 && (
          <p className="mt-0.5 text-xs text-text-tertiary">
            Snoozed {r.snooze_count} {r.snooze_count === 1 ? 'time' : 'times'}; first due{' '}
            {formatDue(r.original_due_at, r.timezone)}
          </p>
        )}
      </div>

      <div className="text-sm">
        <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-text-tertiary">Source</h3>
        {r.source_hidden ? (
          <p className="text-text-tertiary">Source no longer available.</p>
        ) : r.source ? (
          href ? (
            <Link to={href} className="text-brand hover:underline">
              {r.source.title || r.source.kind.replace('_', ' ')}
              {r.source.lines ? ` (lines ${r.source.lines})` : ''}
            </Link>
          ) : (
            <span className="text-text-secondary">{r.source.title || r.source.kind.replace('_', ' ')}</span>
          )
        ) : (
          <p className="text-text-tertiary">No source. A personal reminder.</p>
        )}
      </div>

      {actionError && (
        <p role="alert" className="text-sm text-danger">
          {actionError}
        </p>
      )}

      {open && (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Reminder actions">
          {r.is_owner && (
            <Dropdown
              menuLabel="Snooze options"
              align="left"
              trigger={(isOpen) => (
                <Button size="sm" variant="secondary" aria-haspopup="menu" aria-expanded={isOpen}>
                  Snooze
                </Button>
              )}
              items={[
                { key: '1h', label: '1 hour' },
                { key: 'tomorrow', label: 'Tomorrow 9:00' },
                { key: 'next_week', label: 'Next week' },
                { key: 'custom', label: 'Custom…' },
              ]}
              onSelect={(k) => {
                if (k === 'custom') setModal('snooze');
                else
                  run(snooze, { preset: k as '1h' | 'tomorrow' | 'next_week' }, 'Reminder snoozed');
              }}
            />
          )}
          {r.is_owner && (
            <Button size="sm" variant="secondary" onClick={() => setModal('reschedule')}>
              Reschedule
            </Button>
          )}
          {r.is_creator && (
            <Button size="sm" variant="secondary" onClick={() => setModal('rename')}>
              Edit title
            </Button>
          )}
          {canClose && (
            <Button
              size="sm"
              loading={patch.isPending}
              onClick={() => run(patch, { status: 'done' }, 'Marked done')}
            >
              Done
            </Button>
          )}
          {canClose && (
            <Button size="sm" variant="secondary" onClick={() => setModal('dismiss')}>
              Dismiss
            </Button>
          )}
        </div>
      )}
      {r.is_creator && (
        <Button size="sm" variant="ghost" className="text-danger" onClick={() => setModal('delete')}>
          Delete
        </Button>
      )}

      <Notes id={r.id} />

      <SnoozeDialog
        open={modal === 'snooze'}
        onClose={() => setModal(null)}
        pending={snooze.isPending}
        onSubmit={(until) => run(snooze, { preset: 'custom', until }, 'Reminder snoozed')}
      />
      <RescheduleDialog
        open={modal === 'reschedule'}
        onClose={() => setModal(null)}
        initial={r.due_at}
        pending={patch.isPending}
        onSubmit={(due) =>
          run(patch, { due_at: due, timezone: userTimezone() }, 'Reminder rescheduled')
        }
      />
      <RenameDialog
        open={modal === 'rename'}
        onClose={() => setModal(null)}
        initial={r.title}
        pending={patch.isPending}
        onSubmit={(title) => run(patch, { title }, 'Title updated')}
      />
      <Dialog open={modal === 'dismiss'} onClose={() => setModal(null)} titleId="dismiss-reminder-title">
        <DialogHeader titleId="dismiss-reminder-title">Dismiss this reminder?</DialogHeader>
        <p className="text-sm text-text-secondary">It moves to Dismissed and stays as history.</p>
        <DialogFooter>
          <Button variant="secondary" onClick={() => setModal(null)}>
            Cancel
          </Button>
          <Button
            loading={patch.isPending}
            onClick={() => run(patch, { status: 'dismissed' }, 'Reminder dismissed')}
          >
            Dismiss
          </Button>
        </DialogFooter>
      </Dialog>
      <Dialog open={modal === 'delete'} onClose={() => setModal(null)} titleId="delete-reminder-title">
        <DialogHeader titleId="delete-reminder-title">Delete this reminder?</DialogHeader>
        <p className="text-sm text-text-secondary">This removes it and its notes for everyone.</p>
        <DialogFooter>
          <Button variant="secondary" onClick={() => setModal(null)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={del.isPending}
            onClick={() => run(del, undefined, 'Reminder deleted', onClosed)}
          >
            Delete
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}

function Notes({ id }: { id: string }) {
  const notes = useReminderNotes(id);
  const add = useAddReminderNote(id);
  const [body, setBody] = useState('');
  const [err, setErr] = useState<string | null>(null);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!body.trim()) return;
    setErr(null);
    add.mutate(body.trim(), {
      onSuccess: () => setBody(''),
      onError: (x) => setErr(errorMessage(x)),
    });
  }

  return (
    <section aria-label="Notes">
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-text-tertiary">Notes</h3>
      {notes.isLoading && <p className="text-sm text-text-tertiary">Loading notes…</p>}
      {notes.error && (
        <p role="alert" className="text-sm text-danger">
          {errorMessage(notes.error)}
        </p>
      )}
      {notes.data && notes.data.items.length === 0 && (
        <p className="text-sm text-text-tertiary">No notes yet.</p>
      )}
      <ul className="space-y-2">
        {notes.data?.items.map((n) => (
          <li key={n.id} className="rounded-lg border border-border bg-surface p-3 text-sm">
            <p className="whitespace-pre-wrap text-text-primary">{n.body}</p>
            <p className="mt-1 text-xs text-text-tertiary">
              <RelativeDate iso={n.created_at} />
            </p>
          </li>
        ))}
      </ul>
      <form onSubmit={submit} className="mt-3 space-y-2">
        <Textarea
          title="Add a note"
          value={body}
          rows={3}
          maxLength={4000}
          onChange={(e) => setBody(e.target.value)}
        />
        {err && (
          <p role="alert" className="text-sm text-danger">
            {err}
          </p>
        )}
        <Button type="submit" size="sm" loading={add.isPending} disabled={!body.trim()}>
          Add note
        </Button>
      </form>
    </section>
  );
}

function SnoozeDialog(p: { open: boolean; onClose: () => void; pending: boolean; onSubmit: (until: string) => void }) {
  const [v, setV] = useState('');
  const iso = fromLocalInput(v);
  return (
    <Dialog open={p.open} onClose={p.onClose} titleId="snooze-reminder-title">
      <DialogHeader titleId="snooze-reminder-title">Snooze until</DialogHeader>
      <Input
        title="Date and time"
        type="datetime-local"
        value={v}
        onChange={(e) => setV(e.target.value)}
        hint={`Your time zone: ${userTimezone()}`}
      />
      <DialogFooter>
        <Button variant="secondary" onClick={p.onClose}>
          Cancel
        </Button>
        <Button disabled={!iso} loading={p.pending} onClick={() => iso && p.onSubmit(iso)}>
          Snooze
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function RescheduleDialog(p: {
  open: boolean;
  onClose: () => void;
  initial: string;
  pending: boolean;
  onSubmit: (iso: string) => void;
}) {
  const [v, setV] = useState(() => toLocalInput(p.initial));
  const iso = fromLocalInput(v);
  return (
    <Dialog open={p.open} onClose={p.onClose} titleId="reschedule-reminder-title">
      <DialogHeader titleId="reschedule-reminder-title">Reschedule</DialogHeader>
      <Input
        title="New date and time"
        type="datetime-local"
        value={v}
        onChange={(e) => setV(e.target.value)}
        hint={`Your time zone: ${userTimezone()}`}
      />
      <DialogFooter>
        <Button variant="secondary" onClick={p.onClose}>
          Cancel
        </Button>
        <Button disabled={!iso} loading={p.pending} onClick={() => iso && p.onSubmit(iso)}>
          Reschedule
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function RenameDialog(p: {
  open: boolean;
  onClose: () => void;
  initial: string;
  pending: boolean;
  onSubmit: (title: string) => void;
}) {
  const [v, setV] = useState(p.initial);
  return (
    <Dialog open={p.open} onClose={p.onClose} titleId="rename-reminder-title">
      <DialogHeader titleId="rename-reminder-title">Edit title</DialogHeader>
      <Input title="Title" value={v} maxLength={200} onChange={(e) => setV(e.target.value)} />
      <DialogFooter>
        <Button variant="secondary" onClick={p.onClose}>
          Cancel
        </Button>
        <Button disabled={!v.trim()} loading={p.pending} onClick={() => p.onSubmit(v.trim())}>
          Save
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
