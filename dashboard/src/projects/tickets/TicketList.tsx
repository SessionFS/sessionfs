import { useState } from 'react';
import type { Ticket } from '../../api/client';
import { AssigneeFace } from './AssignMenu';
import { StatusIcon } from './StatusIcon';
import { TicketRow } from './TicketRow';
import type { TicketJiraSurface } from './constants';
import type { ById, TicketGroup } from './views';

/* ── Grouped list ──
 *
 * Sticky, collapsible group headers with counts, each over a card of rows.
 * Rows never render the detail: the open ticket shows in the ticket pane,
 * and its row is marked open. */

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      className={`tk-chev${open ? '' : ' is-collapsed'}`}
    >
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}

export function TicketList({
  groups,
  byId,
  projectId,
  selected,
  onSelect,
  jira,
  hiddenFinished,
  onShowFinished,
  checked,
  onCheck,
  tabStopId,
  onFocusRow,
}: {
  groups: TicketGroup[];
  byId: ById;
  projectId: string;
  selected: string | null;
  onSelect: (id: string) => void;
  jira?: TicketJiraSurface;
  hiddenFinished: number;
  onShowFinished: () => void;
  /** Selected rows (bulk actions). */
  checked: ReadonlySet<string>;
  onCheck: (id: string, range: boolean) => void;
  /** The row whose controls are in the tab order (roving tabindex). */
  tabStopId: string | null;
  onFocusRow: (id: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const single = groups.length === 1 && groups[0].key === 'all';

  return (
    <div className="tk-groups">
      {groups.map((g) => {
        const isCollapsed = collapsed.has(g.key);
        const headId = `tk-group-${g.key}`;
        return (
          <section key={g.key} className="tk-group" aria-labelledby={single ? undefined : headId}>
            {!single && (
              <h3 className="tk-ghead-wrap">
                <button
                  type="button"
                  id={headId}
                  className="tk-ghead"
                  aria-expanded={!isCollapsed}
                  onClick={() => toggle(g.key)}
                >
                  <Chevron open={!isCollapsed} />
                  {g.icon?.kind === 'status' && <StatusIcon status={g.icon.status} size={14} />}
                  {g.icon?.kind === 'assignee' && <AssigneeFace ticket={g.icon.ticket} />}
                  <span className="tk-ghead-label">{g.label}</span>
                  <span className="tk-ghead-n">
                    {g.rows.length}
                    <span className="sr-only"> {g.rows.length === 1 ? 'ticket' : 'tickets'}</span>
                  </span>
                  {g.sub && <span className="tk-ghead-sub">· {g.sub}</span>}
                </button>
              </h3>
            )}
            {!isCollapsed && (
              <ul className="tk-rows" aria-label={single ? 'Tickets' : g.label}>
                {g.rows.map(({ ticket, child }) => (
                  <ListItem
                    key={`${g.key}:${ticket.id}`}
                    ticket={ticket}
                    child={!!child}
                    byId={byId}
                    projectId={projectId}
                    expanded={selected === ticket.id}
                    onToggle={() => onSelect(ticket.id)}
                    jira={jira}
                    checked={checked.has(ticket.id)}
                    onCheck={(range) => onCheck(ticket.id, range)}
                    tabbable={tabStopId === ticket.id}
                    onFocusRow={() => onFocusRow(ticket.id)}
                  />
                ))}
              </ul>
            )}
          </section>
        );
      })}
      {hiddenFinished > 0 && (
        <button type="button" className="tk-finished-fold" onClick={onShowFinished}>
          <Chevron open={false} />
          {hiddenFinished} finished {hiddenFinished === 1 ? 'ticket' : 'tickets'} hidden · show
        </button>
      )}
    </div>
  );
}

function ListItem({
  ticket,
  child,
  byId,
  projectId,
  expanded,
  onToggle,
  jira,
  checked,
  onCheck,
  tabbable,
  onFocusRow,
}: {
  ticket: Ticket;
  child: boolean;
  byId: ById;
  projectId: string;
  expanded: boolean;
  onToggle: () => void;
  jira?: TicketJiraSurface;
  checked: boolean;
  onCheck: (range: boolean) => void;
  tabbable: boolean;
  onFocusRow: () => void;
}) {
  return (
    <li className="tk-li">
      <TicketRow
        ticket={ticket}
        byId={byId}
        projectId={projectId}
        child={child}
        expanded={expanded}
        onToggle={onToggle}
        orgId={jira?.orgId}
        isProjectAdmin={jira?.isProjectAdmin}
        checked={checked}
        onCheck={onCheck}
        tabbable={tabbable}
        onFocusRow={onFocusRow}
      />
    </li>
  );
}
