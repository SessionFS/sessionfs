import RelativeDate from '../../components/RelativeDate';
import { PRIORITY_TONE, type ViewProps } from './constants';
import { KindBadge } from './KindBadge';
import { StatusCell } from './StatusCell';
import { TicketDetail } from './TicketDetail';

export function ListView({ tickets, projectId, selected, onSelect, onNavigate }: ViewProps) {
  return (
    <ul className="border border-border rounded-lg divide-y divide-[var(--border)]">
      {tickets.map((t) => (
        <li key={t.id}>
          {/* Row container is NOT interactive. The expand trigger (title/meta)
              is a <button>; the StatusCell action is a SIBLING, not a
              descendant — so no interactive control nests inside a button. */}
          <div
            className={`px-3 py-2 hover:bg-surface flex items-start gap-3 border-l-2 ${
              t.kind === 'issue' ? 'border-info/60' : 'border-transparent'
            }`}
          >
            <button
              type="button"
              onClick={() => onSelect(t.id)}
              aria-expanded={selected === t.id}
              className="flex-1 min-w-0 text-left cursor-pointer rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              <div className="flex items-center gap-2 text-sm flex-wrap">
                <span className="text-mono-chip">{t.id}</span>
                <KindBadge kind={t.kind} />
                <span
                  className={`text-xs uppercase tracking-wide ${PRIORITY_TONE[t.priority] ?? ''}`}
                >
                  {t.priority}
                </span>
                {t.assigned_to && (
                  <span className="text-xs text-text-tertiary">
                    → {t.assigned_to}
                  </span>
                )}
              </div>
              <div className="font-medium truncate text-text-primary">
                {t.title}
              </div>
              <div className="text-xs text-text-tertiary">
                <RelativeDate iso={t.updated_at} /> · {t.acceptance_criteria.length}{' '}
                criteria
                {t.depends_on.length > 0 ? ` · ${t.depends_on.length} deps` : ''}
                {t.kind === 'issue' && t.child_ticket_ids.length > 0
                  ? ` · ${t.child_ticket_ids.length} child ${
                      t.child_ticket_ids.length === 1 ? 'task' : 'tasks'
                    }`
                  : ''}
              </div>
            </button>
            {/* Independent status action — sibling of the expand button. */}
            <div className="shrink-0">
              <StatusCell ticket={t} projectId={projectId} />
            </div>
          </div>
          {selected === t.id && (
            <TicketDetail
              projectId={projectId}
              ticketId={t.id}
              fallback={t}
              onNavigate={onNavigate}
            />
          )}
        </li>
      ))}
    </ul>
  );
}
