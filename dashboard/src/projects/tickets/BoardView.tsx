import type { Ticket } from '../../api/client';
import { AssigneeFace } from './AssignMenu';
import { Signals } from './Signals';
import { PriorityIcon, StatusIcon } from './StatusIcon';
import { assigneeName, boardColumns, dueDisplay, isFinished, priorityText, type ById, type SortBy } from './views';

/* ── Board ──
 *
 * Six columns (Triage, Open, In progress, Blocked, In review, Done) that
 * snap as they scroll. Cancelled tickets are never a column: a short note
 * says how many there are and opens them in the list. There is no drag and
 * drop: start, complete, block and resolve run where the work runs, so the
 * board only reads. A card opens the ticket pane (a slide-over here). */

export function BoardView({
  tickets,
  byId,
  sort,
  selected,
  onSelect,
  tabStopId,
  onFocusCard,
  finishedHidden,
  cancelledCount,
  onShowCancelled,
}: {
  /** Tickets on screen (cancelled ones are left out here). */
  tickets: Ticket[];
  byId: ById;
  sort: SortBy;
  selected: string | null;
  onSelect: (id: string) => void;
  /** The one card in the tab order (roving tabindex). */
  tabStopId: string | null;
  onFocusCard: (id: string) => void;
  /** Finished tickets are hidden by the view (All without Show finished). */
  finishedHidden: boolean;
  cancelledCount: number;
  onShowCancelled: () => void;
}) {
  const columns = boardColumns(
    tickets.filter((t) => t.status !== 'cancelled'),
    sort,
  );
  return (
    <div className="tk-board">
      {columns.map(({ column, tickets: items }) => {
        const headId = `tk-col-${column.key}`;
        return (
          <section key={column.key} className="tk-col" aria-labelledby={headId}>
            <h3 id={headId} className="tk-col-head">
              <StatusIcon status={column.status} size={14} />
              {column.label}
              <span className="tk-col-n">
                {items.length}
                <span className="sr-only"> {items.length === 1 ? 'ticket' : 'tickets'}</span>
              </span>
            </h3>
            {column.note && <p className="tk-col-note">{column.note}</p>}
            {items.length ? (
              <ul className="tk-cards">
                {items.map((t) => (
                  <li key={t.id}>
                    <BoardCard
                      ticket={t}
                      byId={byId}
                      open={selected === t.id}
                      tabbable={tabStopId === t.id}
                      onOpen={() => onSelect(t.id)}
                      onFocus={() => onFocusCard(t.id)}
                    />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="tk-col-empty">
                {column.key === 'done' && finishedHidden ? 'Finished tickets are hidden. Turn on Show finished.' : 'Nothing here'}
              </p>
            )}
          </section>
        );
      })}
      {cancelledCount > 0 && (
        <section className="tk-col tk-col-aside" aria-labelledby="tk-col-cancelled">
          <h3 id="tk-col-cancelled" className="tk-col-head">
            <StatusIcon status="cancelled" size={14} />
            Cancelled
            <span className="tk-col-n">{cancelledCount}</span>
          </h3>
          <p className="tk-col-note">Not shown on the board.</p>
          <button type="button" className="tk-col-link" onClick={onShowCancelled}>
            Show in the list
          </button>
        </section>
      )}
    </div>
  );
}

function BoardCard({
  ticket: t,
  byId,
  open,
  tabbable,
  onOpen,
  onFocus,
}: {
  ticket: Ticket;
  byId: ById;
  open: boolean;
  tabbable: boolean;
  onOpen: () => void;
  onFocus: () => void;
}) {
  const due = dueDisplay(t);
  const who = assigneeName(t);
  const hasAssignee = !!(t.assigned_to || t.assigned_user_id || t.assigned_contact);
  return (
    <button
      type="button"
      className={`tk-card${open ? ' is-open' : ''}${isFinished(t) ? ' is-dim' : ''}`}
      onClick={onOpen}
      onFocus={onFocus}
      tabIndex={tabbable ? 0 : -1}
      aria-haspopup="dialog"
      aria-expanded={open}
      data-ticket-row={t.id}
    >
      <span className="tk-card-meta">
        <span className="tk-id-text">{t.id}</span>
        {t.kind === 'issue' && <span className="tk-kind">Issue</span>}
      </span>
      <span className="tk-card-title">{t.title}</span>
      <Sigs ticket={t} byId={byId} />
      <span className="tk-card-foot">
        <span className="tk-prio" title={`Priority: ${priorityText(t.priority)}`}>
          <PriorityIcon priority={t.priority} />
          <span className="sr-only">Priority {priorityText(t.priority)}</span>
        </span>
        {due && (
          <span className={`tk-due${due.tone ? ` is-${due.tone}` : ''}`} title={`Due ${due.full}`}>
            <span className="sr-only">Due </span>
            {due.text}
          </span>
        )}
        <span className="tk-card-sp" />
        {hasAssignee && (
          <span className="tk-card-asg" title={who ?? undefined}>
            <AssigneeFace ticket={t} />
            <span className="sr-only">Assigned to {who ?? 'a removed contact'}</span>
          </span>
        )}
      </span>
    </button>
  );
}

/** The row signals, on their own line (hidden by CSS when there are none). */
function Sigs({ ticket, byId }: { ticket: Ticket; byId: ById }) {
  return (
    <span className="tk-card-sig">
      <Signals ticket={ticket} byId={byId} compact />
    </span>
  );
}
