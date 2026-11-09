import type { Ticket } from '../../../api/client';
import { Button } from '../../../components/ui';
import { TicketLinkRow } from './TicketLinkRow';

const SEGMENTS: { key: string; label: string; statuses: string[]; color: string }[] = [
  { key: 'done', label: 'finished', statuses: ['done', 'closed', 'cancelled'], color: 'var(--success)' },
  { key: 'review', label: 'in review', statuses: ['review'], color: 'var(--cat-mention)' },
  { key: 'progress', label: 'in progress', statuses: ['in_progress'], color: 'var(--brand)' },
  { key: 'blocked', label: 'blocked', statuses: ['blocked'], color: 'var(--danger)' },
  { key: 'open', label: 'not started', statuses: ['open', 'suggested'], color: 'var(--border-strong)' },
];

export function ChildrenTab({
  children,
  onOpenTicket,
  onAddChild,
}: {
  /** child_ticket_ids with whatever has loaded for each. */
  children: { id: string; ticket?: Ticket; failed?: boolean }[];
  onOpenTicket: (id: string) => void;
  /** Absent when the viewer can't file tickets here. */
  onAddChild?: () => void;
}) {
  const total = children.length;
  const loaded = children.map((c) => c.ticket).filter((t): t is Ticket => !!t);
  const counts = SEGMENTS.map((s) => ({ ...s, n: loaded.filter((t) => s.statuses.includes(t.status)).length }));
  const done = counts[0].n;

  return (
    <div className="space-y-4 text-sm">
      {total === 0 ? (
        <p className="text-xs text-text-tertiary">
          No child tasks yet. File a Task with this Issue as its parent and it shows up here.
        </p>
      ) : (
        <>
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <span className="text-md font-semibold tabular-nums text-text-primary">
                {done}/{total}
              </span>
              <span className="sr-only">child tasks finished</span>
              <span
                className="flex h-2 flex-1 overflow-hidden rounded-full bg-surface-hover"
                role="img"
                aria-label={counts
                  .filter((c) => c.n)
                  .map((c) => `${c.n} ${c.label}`)
                  .join(', ')}
              >
                {counts.map((c) =>
                  c.n ? (
                    <i key={c.key} className="block h-full" style={{ width: `${(c.n / total) * 100}%`, backgroundColor: c.color }} />
                  ) : null,
                )}
              </span>
            </div>
            <ul className="flex flex-wrap gap-x-3 gap-y-1 text-2xs text-text-tertiary" aria-hidden="true">
              {counts
                .filter((c) => c.n)
                .map((c) => (
                  <li key={c.key} className="inline-flex items-center gap-1.5">
                    <i className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: c.color }} />
                    {c.n} {c.label}
                  </li>
                ))}
            </ul>
          </div>
          <ul className="space-y-1.5" aria-label="Child tasks">
            {children.map((c) => (
              <li key={c.id}>
                <TicketLinkRow id={c.id} ticket={c.ticket} failed={c.failed} onOpen={onOpenTicket} />
              </li>
            ))}
          </ul>
        </>
      )}
      {onAddChild && (
        <Button variant="secondary" size="sm" onClick={onAddChild}>
          Add child task
        </Button>
      )}
    </div>
  );
}
