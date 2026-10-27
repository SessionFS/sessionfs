import type { Ticket } from '../../api/client';
import { Drawer } from '../../components/ui';
import { BOARD_STATUSES, PRIORITY_TONE, type ViewProps } from './constants';
import { KindBadge } from './KindBadge';
import { TicketDetail } from './TicketDetail';

/* ── Board view ──
 *
 * Columns are derived from the statuses the tab already knows. There is
 * NO drag-and-drop: every FSM transition that changes status
 * (start/complete/resolve/block/escalate) is CLI/MCP-only and must not
 * be draggable. The only dashboard-side transitions are button actions
 * (approve/dismiss/close) which appear as explicit buttons inside the
 * expanded detail — not via drag. */

export function BoardView({ tickets, projectId, selected, onSelect, onNavigate, jira }: ViewProps) {
  const selectedTicket = tickets.find((t) => t.id === selected) ?? null;

  return (
    <>
      <div className="overflow-x-auto -mx-1 px-1">
        <div className="flex gap-3 min-w-[980px]">
          {BOARD_STATUSES.map((status) => {
            const columnTickets = tickets.filter((t) => t.status === status.value);
            return (
              <div key={status.value} className="flex-1 min-w-[130px] max-w-[260px]">
                {/* Column header */}
                <h3 className="text-micro uppercase font-semibold text-text-tertiary mb-2 px-1 flex items-center gap-1.5">
                  {status.label}
                  <span className="text-text-tertiary/60 tabular-nums">
                    {columnTickets.length}
                  </span>
                </h3>

                {columnTickets.length === 0 ? (
                  <div className="border border-dashed border-border rounded-lg p-3 text-xs text-text-tertiary text-center">
                    —
                  </div>
                ) : (
                  <div className="space-y-2">
                    {columnTickets.map((t) => (
                      <BoardCard
                        key={t.id}
                        ticket={t}
                        isExpanded={selected === t.id}
                        onToggle={() => onSelect(t.id)}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Board detail drawer — right-side panel instead of in-column expansion */}
      <Drawer
        open={selected !== null}
        onClose={() => onSelect(selected!)}
        titleId="ticket-detail-title"
      >
        {selectedTicket && (
          <div className="space-y-3">
            <h2
              id="ticket-detail-title"
              className="text-lg font-semibold text-text-primary"
            >
              {selectedTicket.title}
            </h2>
            <TicketDetail
              projectId={projectId}
              ticketId={selectedTicket.id}
              fallback={selectedTicket}
              onNavigate={onNavigate}
              orgId={jira?.orgId}
              isProjectAdmin={jira?.isProjectAdmin}
              isEnterprise={jira?.isEnterprise}
              bare
            />
          </div>
        )}
      </Drawer>
    </>
  );
}

/* ── Board card ── */

function BoardCard({
  ticket: t,
  isExpanded,
  onToggle,
}: {
  ticket: Ticket;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`w-full text-left bg-surface border border-border rounded-lg p-3 hover:bg-bg-elevated hover:border-border-strong transition-[background-color,border-color] duration-150 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
        isExpanded ? 'border-[var(--brand)] ring-1 ring-[var(--brand)]/30' : ''
      }`}
      aria-expanded={isExpanded}
      aria-haspopup="dialog"
    >
      {/* Kind + ID row */}
      <div className="flex items-center gap-1.5 mb-1.5 flex-wrap">
        <KindBadge kind={t.kind} />
        <span className="text-mono-chip text-2xs">{t.id.slice(0, 12)}</span>
      </div>

      {/* Title */}
      <div className="text-sm font-medium text-text-primary mb-2 leading-snug line-clamp-2">
        {t.title}
      </div>

      {/* Meta row */}
      <div className="flex items-center gap-1.5 flex-wrap text-2xs text-text-tertiary">
        <span className={`uppercase tracking-wide font-semibold ${PRIORITY_TONE[t.priority] ?? ''}`}>
          {t.priority}
        </span>
        {t.assigned_to && (
          <span className="text-mono-chip text-2xs">{t.assigned_to}</span>
        )}
        {t.acceptance_criteria.length > 0 && (
          <>
            <span className="opacity-40">·</span>
            <span className="tabular-nums">{t.acceptance_criteria.length} crit</span>
          </>
        )}
        {t.depends_on.length > 0 && (
          <>
            <span className="opacity-40">·</span>
            <span className="tabular-nums">{t.depends_on.length} dep</span>
          </>
        )}
      </div>
    </button>
  );
}
