import { Link } from 'react-router-dom';
import { Badge } from '../components/Badge';
import { useSourceReminders } from '../hooks/useReminders';
import { formatDue, reminderHref } from './format';

/** Reminders on a meeting or ticket (the viewer's own), hidden when none.
 *  A group meeting passes ``groupId`` instead of ``projectId``. */
export function RemindersStrip({
  kind,
  projectId,
  groupId,
  sourceId,
}: {
  kind: 'meeting' | 'ticket';
  projectId: string | undefined;
  groupId?: string;
  sourceId: string | undefined;
}) {
  const { data } = useSourceReminders(kind, projectId, sourceId, groupId);
  if (!data || data.items.length === 0) return null;
  const c = data.counts;
  return (
    <section aria-label="Reminders" className="rounded-lg border border-border bg-surface p-3">
      <h3 className="text-sm font-medium text-text-primary">
        Reminders{' '}
        <span className="font-normal text-text-tertiary">
          {c.open} open, {c.done} done, {c.dismissed} dismissed
        </span>
      </h3>
      <ul className="mt-2 space-y-1">
        {data.items.map((r) => (
          <li key={r.id} className="flex items-center justify-between gap-3 text-sm">
            <Link to={reminderHref(r.id)} className="min-w-0 truncate text-text-primary hover:text-brand">
              {r.title}
            </Link>
            <span className="flex shrink-0 items-center gap-2 text-xs text-text-tertiary">
              {r.status !== 'open' ? (
                <Badge variant="default" tint label={r.status === 'done' ? 'Done' : 'Dismissed'} size="sm" />
              ) : r.is_due ? (
                <Badge variant="warning" tint label="Due" size="sm" />
              ) : null}
              {formatDue(r.due_at, r.timezone)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
