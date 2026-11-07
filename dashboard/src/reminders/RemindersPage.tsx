import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { Reminder, ReminderStatus } from '../api/client';
import { Badge } from '../components/Badge';
import { Button, Card, PageContainer, PageHeader, Tabs } from '../components/ui';
import { useReminders } from '../hooks/useReminders';
import { errorMessage } from '../utils/errorMessage';
import { AddReminderDialog } from './AddReminderDialog';
import { formatDue, sortReminders } from './format';
import { ReminderDrawer } from './ReminderDrawer';

const TABS: { key: ReminderStatus; label: string }[] = [
  { key: 'open', label: 'Open' },
  { key: 'done', label: 'Done' },
  { key: 'dismissed', label: 'Dismissed' },
];

export default function RemindersPage() {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<ReminderStatus>('open');
  const [adding, setAdding] = useState(false);
  const openId = params.get('reminder');

  function openReminder(id: string | null) {
    const next = new URLSearchParams(params);
    if (id) next.set('reminder', id);
    else next.delete('reminder');
    setParams(next, { replace: !id });
  }

  return (
    <PageContainer width="narrow" className="space-y-5">
      <PageHeader
        title="Reminders"
        description="Things you asked to be reminded about. Only you and the person who set one can see it."
        actions={<Button size="sm" onClick={() => setAdding(true)}>Add reminder</Button>}
      />
      <Tabs
        aria-label="Reminder status"
        activeKey={status}
        onChange={(k) => setStatus(k as ReminderStatus)}
        tabs={TABS.map((t) => ({
          key: t.key,
          label: t.label,
          content: <ReminderList status={t.key} onOpen={openReminder} />,
        }))}
      />
      <AddReminderDialog open={adding} onClose={() => setAdding(false)} />
      <ReminderDrawer reminderId={openId} onClose={() => openReminder(null)} />
    </PageContainer>
  );
}

function ReminderList({ status, onOpen }: { status: ReminderStatus; onOpen: (id: string) => void }) {
  const { data, isLoading, error } = useReminders(status);
  if (isLoading) return <p className="pt-4 text-sm text-text-tertiary">Loading reminders…</p>;
  if (error) {
    return (
      <p role="alert" className="pt-4 text-sm text-danger">
        Failed to load reminders: {errorMessage(error)}
      </p>
    );
  }
  const items = sortReminders(data?.items ?? []);
  if (items.length === 0) {
    return <p className="pt-4 text-sm text-text-tertiary">No {status} reminders.</p>;
  }
  return (
    <ul aria-label={`${status} reminders`} className="space-y-2 pt-4">
      {items.map((r) => (
        <li key={r.id}>
          <ReminderRow reminder={r} onOpen={onOpen} />
        </li>
      ))}
    </ul>
  );
}

function ReminderRow({ reminder: r, onOpen }: { reminder: Reminder; onOpen: (id: string) => void }) {
  const due = r.status === 'open' && r.is_due;
  return (
    <Card className={`p-4 ${due ? 'border-l-2 border-l-warning' : ''}`} data-testid={`reminder-${r.id}`}>
      <button
        type="button"
        onClick={() => onOpen(r.id)}
        className="flex w-full items-start justify-between gap-3 text-left outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded"
      >
        <span className="min-w-0">
          <span className="block truncate font-medium text-text-primary">{r.title}</span>
          <span className="mt-0.5 block text-xs text-text-tertiary">{formatDue(r.due_at, r.timezone)}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {r.note_count > 0 && (
            <span className="text-xs text-text-tertiary">
              {r.note_count} {r.note_count === 1 ? 'note' : 'notes'}
            </span>
          )}
          {due && <Badge variant="warning" tint label="Due" size="sm" />}
        </span>
      </button>
    </Card>
  );
}
