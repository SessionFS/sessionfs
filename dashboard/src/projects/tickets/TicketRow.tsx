import type { Ticket } from '../../api/client';
import { formatDay } from './schedule';
import { AssigneeCell } from './AssignMenu';
import { Signals } from './Signals';
import { PriorityIcon } from './StatusIcon';
import { StatusMenu } from './StatusMenu';
import { dueDisplay, isFinished, priorityText, shortAge, type ById } from './views';

/* ── Ticket row ──
 *
 * One 40px line: status (a menu button), id, title, signals, due, priority,
 * assignee (a reassign button when allowed) and age. The title is the
 * button that opens the ticket pane (data-ticket-row marks it, so closing
 * the pane can hand focus back); a pseudo-element stretches it over the row so the whole
 * line opens the ticket, while the status and assignee buttons sit above it
 * as siblings (no control is nested in another). Narrow containers drop the
 * quieter columns (tickets.css). */

export function TicketRow({
  ticket: t,
  byId,
  projectId,
  child = false,
  expanded,
  onToggle,
  orgId,
  isProjectAdmin,
  checked = false,
  onCheck,
  tabbable = true,
  onFocusRow,
}: {
  ticket: Ticket;
  byId: ById;
  projectId: string;
  child?: boolean;
  expanded: boolean;
  onToggle: () => void;
  orgId?: string | null;
  isProjectAdmin?: boolean;
  /** Row selection for bulk actions; no checkbox without `onCheck`. */
  checked?: boolean;
  /** Toggle this row; `range` = shift-click (select from the last row
   *  clicked to this one). */
  onCheck?: (range: boolean) => void;
  /** Roving tabindex: only one row's controls are in the tab order. */
  tabbable?: boolean;
  onFocusRow?: () => void;
}) {
  const tab = tabbable ? 0 : -1;
  const due = dueDisplay(t);
  const suggestedBy = t.status === 'suggested' ? t.created_by_persona || t.created_by?.display_name || null : null;
  return (
    <div
      className={`tk-row${child ? ' is-child' : ''}${isFinished(t) ? ' is-dim' : ''}${expanded ? ' is-open' : ''}${checked ? ' is-sel' : ''}${onCheck ? '' : ' no-cb'}`}
      data-ticket-id={t.id}
      onFocus={onFocusRow}
    >
      {onCheck && (
        <input
          type="checkbox"
          className="tk-cb"
          checked={checked}
          tabIndex={tab}
          aria-label={`Select ${t.title}`}
          onChange={(e) => onCheck(!!(e.nativeEvent as MouseEvent).shiftKey)}
        />
      )}
      <StatusMenu ticket={t} projectId={projectId} tabIndex={tab} />
      <span className="tk-id">
        <span className="tk-id-text">{t.id}</span>
        <span className="tk-msig">
          <Signals ticket={t} byId={byId} compact />
        </span>
      </span>
      <span className="tk-title-cell">
        {child && <span className="tk-child-mark" aria-hidden="true" />}
        {t.kind === 'issue' && <span className="tk-kind">Issue</span>}
        <button
          type="button"
          className="tk-title"
          aria-expanded={expanded}
          aria-current={expanded ? 'true' : undefined}
          data-ticket-row={t.id}
          tabIndex={tab}
          onClick={(e) => {
            // Shift-click selects a range instead of opening the ticket.
            if (e.shiftKey && onCheck) onCheck(true);
            else onToggle();
          }}
        >
          {t.title}
        </button>
        {suggestedBy && <span className="tk-by">by {suggestedBy}</span>}
      </span>
      <span className="tk-sig">
        <Signals ticket={t} byId={byId} />
      </span>
      <span className={`tk-due${due?.tone ? ` is-${due.tone}` : ''}`} title={due ? `Due ${due.full}` : undefined}>
        {due && (
          <>
            <span className="sr-only">Due </span>
            {due.text}
          </>
        )}
      </span>
      <span className="tk-prio" title={`Priority: ${priorityText(t.priority)}`}>
        <PriorityIcon priority={t.priority} />
        <span className="sr-only">Priority {priorityText(t.priority)}</span>
      </span>
      <span className="tk-asgcell">
        <AssigneeCell ticket={t} projectId={projectId} orgId={orgId} isProjectAdmin={isProjectAdmin} tabIndex={tab} />
      </span>
      <span className="tk-age" title={`Updated ${formatDay(t.updated_at)}`}>
        <span className="sr-only">Updated </span>
        {shortAge(t.updated_at)}
      </span>
    </div>
  );
}
