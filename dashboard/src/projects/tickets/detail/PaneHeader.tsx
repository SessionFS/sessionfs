import type { ReactNode, Ref } from 'react';
import type { Ticket } from '../../../api/client';
import { Dropdown, IconButton } from '../../../components/ui';
import { AssigneeLabel } from '../AssigneeLabel';
import { formatDueDate, isOverdue } from '../schedule';
import { PaneIcon } from './icons';
import { StatusIcon } from '../StatusIcon';
import { StatusMenu } from '../StatusMenu';

/* ── Pane header ──
 *
 * Identity row (status, id, kind, Jira key, prev/next, follow, close), the
 * parent Issue, the title, then one row of properties. A property is a
 * button only when the viewer can change it; otherwise it is plain text. */

export type PaneMode = 'split' | 'drawer' | 'phone';

const PRIORITIES = ['low', 'medium', 'high', 'critical'] as const;

const PROP =
  'inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-text-secondary';
const PROP_BUTTON = `${PROP} cursor-pointer hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`;

function PriorityBars({ priority }: { priority: string }) {
  const level = Math.max(0, PRIORITIES.indexOf(priority as (typeof PRIORITIES)[number])) + 1;
  const tone = priority === 'critical' ? 'text-danger' : priority === 'high' ? 'text-warning' : 'text-text-secondary';
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false" className={tone}>
      {[0, 1, 2, 3].map((i) => (
        <rect
          key={i}
          x={i * 3}
          y={9 - i * 2.4}
          width="2"
          height={3 + i * 2.4}
          rx="0.5"
          fill="currentColor"
          opacity={i < level ? 1 : 0.25}
        />
      ))}
    </svg>
  );
}

/** Escape on an open menu closes only the menu. The slide-over listens on
 *  the document and skips handled events, so claim it here first (React's
 *  root listener runs before document listeners). */
function MenuEscape({ children }: { children: ReactNode }) {
  return (
    <span
      className="contents"
      onKeyDown={(e) => {
        if (e.key !== 'Escape') return;
        // The row popover (status menu) skips handled events, so close it here.
        const pop = e.currentTarget.querySelector<HTMLElement>('.tk-pop[data-open]');
        if (pop) {
          e.preventDefault();
          const trigger = pop.querySelector<HTMLButtonElement>('[aria-expanded="true"]');
          trigger?.click();
          trigger?.focus();
        } else if (e.currentTarget.querySelector('[role="menu"]')) {
          // ui/Dropdown closes itself on any Escape.
          e.preventDefault();
        }
      }}
    >
      {children}
    </span>
  );
}

export interface PaneHeaderProps {
  projectId: string;
  ticket: Ticket;
  titleId: string;
  titleRef: Ref<HTMLHeadingElement>;
  mode: PaneMode;
  prevId: string | null;
  nextId: string | null;
  onStep: (id: string) => void;
  onClose: () => void;
  parent: Ticket | null | undefined;
  parentProgress: { done: number; total: number } | null;
  onOpenTicket: (id: string) => void;
  watching: boolean;
  muted: boolean;
  followBusy: boolean;
  onToggleFollow: () => void;
  onCopyId: () => void;
  onOpenJira: (() => void) | null;
  canReassign: boolean;
  reassigning: boolean;
  onReassign: () => void;
  reassignRef: Ref<HTMLButtonElement>;
  canEditPriority: boolean;
  onPriority: (p: string) => void;
  canEditSchedule: boolean;
  onEditSchedule: (field: 'due date' | 'estimate') => void;
  /** Rendered under the properties (the reassign editor, a confirm). */
  below?: ReactNode;
}

export function PaneHeader({ titleRef, reassignRef, ...p }: PaneHeaderProps) {
  const t = p.ticket;
  const hasAssignee = !!(t.assigned_to || t.assigned_user_id || t.assigned_contact_id);
  const overdue = isOverdue(t);

  return (
    <div className="space-y-3">
      <div className="flex min-w-0 items-center gap-1.5">
        {p.mode === 'phone' && (
          <IconButton label="Back to tickets" variant="ghost" size="sm" onClick={p.onClose}>
            {<PaneIcon name="back" size={16} />}
          </IconButton>
        )}
        <StatusIcon status={t.status} size={14} />
        <span className="truncate font-mono text-xs text-text-tertiary">{t.id}</span>
        <IconButton label="Copy ticket id" variant="ghost" size="sm" onClick={p.onCopyId}>
          {<PaneIcon name="copy" size={13} />}
        </IconButton>
        {t.kind === 'issue' && (
          <span className="inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide bg-info/15 text-info">
            Issue
          </span>
        )}
        {t.jira && p.onOpenJira && (
          <button
            type="button"
            onClick={p.onOpenJira}
            aria-label={`Jira issue ${t.jira.issue_key}: open the Jira tab`}
            className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-1.5 py-0.5 font-mono text-2xs text-text-secondary hover:bg-surface-hover outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
          >
            {t.jira.issue_key}
          </button>
        )}
        <span className="flex-1" />
        <IconButton
          label="Previous ticket"
          variant="ghost"
          size="sm"
          disabled={!p.prevId}
          onClick={() => p.prevId && p.onStep(p.prevId)}
        >
          {<PaneIcon name="up" size={16} />}
        </IconButton>
        <IconButton
          label="Next ticket"
          variant="ghost"
          size="sm"
          disabled={!p.nextId}
          onClick={() => p.nextId && p.onStep(p.nextId)}
        >
          {<PaneIcon name="down" size={16} />}
        </IconButton>
        <IconButton
          label={p.watching ? (p.muted ? 'Following, muted: unfollow' : 'Following: unfollow') : 'Follow this ticket'}
          variant="ghost"
          size="sm"
          aria-pressed={p.watching}
          disabled={p.followBusy}
          onClick={p.onToggleFollow}
          className={p.watching ? '!text-brand' : undefined}
        >
          {<PaneIcon name="bell" size={16} />}
        </IconButton>
        {p.mode !== 'phone' && (
          <IconButton label="Close ticket" variant="ghost" size="sm" onClick={p.onClose}>
            {<PaneIcon name="close" size={16} />}
          </IconButton>
        )}
      </div>

      {t.parent_ticket_id && (
        <button
          type="button"
          onClick={() => p.onOpenTicket(t.parent_ticket_id!)}
          className="flex max-w-full min-w-0 items-center gap-1.5 rounded text-xs text-text-tertiary hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          {<PaneIcon name="tree" size={13} />}
          <span className="sr-only">Parent Issue:</span>
          {p.parent ? (
            <span className="truncate">{p.parent.title}</span>
          ) : (
            <span className="truncate font-mono">{t.parent_ticket_id}</span>
          )}
          {p.parentProgress && (
            <span className="shrink-0 font-mono tabular-nums">
              {p.parentProgress.done}/{p.parentProgress.total}
              <span className="sr-only"> child tasks finished</span>
            </span>
          )}
        </button>
      )}

      <h2
        id={p.titleId}
        ref={titleRef}
        tabIndex={-1}
        className="text-lg font-semibold leading-snug text-text-primary outline-none [overflow-wrap:anywhere]"
      >
        {t.title}
      </h2>

      <div className="flex flex-wrap items-center gap-1.5">
        <MenuEscape>
          <StatusMenu ticket={t} projectId={p.projectId} showLabel />
        </MenuEscape>

        {p.canReassign ? (
          <button
            ref={reassignRef}
            data-tk-assign=""
            type="button"
            aria-expanded={p.reassigning}
            onClick={p.onReassign}
            className={PROP_BUTTON}
          >
            <span className="sr-only">{hasAssignee ? 'Reassign, assigned to ' : 'Assign, '}</span>
            {hasAssignee ? <AssigneeLabel ticket={t} emphasis /> : <span className="text-text-tertiary">Unassigned</span>}
          </button>
        ) : (
          <span className={PROP}>
            <span className="sr-only">Assignee: </span>
            {hasAssignee ? <AssigneeLabel ticket={t} emphasis /> : <span className="text-text-tertiary">Unassigned</span>}
          </span>
        )}

        {p.canEditPriority ? (
          <MenuEscape>
          <Dropdown
            align="left"
            menuLabel="Change priority"
            trigger={(open) => (
              <button
                type="button"
                aria-haspopup="menu"
                aria-expanded={open}
                aria-label={`Priority: ${t.priority}. Change priority`}
                className={PROP_BUTTON}
              >
                <PriorityBars priority={t.priority} />
                <span className="capitalize">{t.priority}</span>
              </button>
            )}
            items={PRIORITIES.map((v) => ({ key: v, label: v.charAt(0).toUpperCase() + v.slice(1), disabled: v === t.priority }))}
            onSelect={p.onPriority}
          />
          </MenuEscape>
        ) : (
          <span className={PROP}>
            <PriorityBars priority={t.priority} />
            <span className="sr-only">Priority: </span>
            <span className="capitalize">{t.priority}</span>
          </span>
        )}

        {(t.due_date || p.canEditSchedule) &&
          (p.canEditSchedule ? (
            <button type="button" className={PROP_BUTTON} onClick={() => p.onEditSchedule('due date')}>
              {t.due_date ? (
                <span className={overdue ? 'font-medium text-danger' : undefined}>
                  {overdue ? 'Overdue' : 'Due'} {formatDueDate(t.due_date)}
                  <span className="sr-only">. Change due date</span>
                </span>
              ) : (
                <span className="text-text-tertiary">Add due date</span>
              )}
            </button>
          ) : (
            <span className={PROP}>
              <span className={overdue ? 'font-medium text-danger' : undefined}>
                {overdue ? 'Overdue' : 'Due'} {formatDueDate(t.due_date!)}
              </span>
            </span>
          ))}

        {t.estimated_effort &&
          (p.canEditSchedule ? (
            <button type="button" className={PROP_BUTTON} onClick={() => p.onEditSchedule('estimate')}>
              Est. <span className="font-mono text-text-primary">{t.estimated_effort}</span>
              <span className="sr-only">. Change estimate</span>
            </button>
          ) : (
            <span className={PROP}>
              Est. <span className="font-mono text-text-primary">{t.estimated_effort}</span>
            </span>
          ))}
      </div>

      {p.below}
    </div>
  );
}
