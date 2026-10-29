import { Link } from 'react-router-dom';
import type { MeetingActionItem, MeetingParticipant } from '../api/client';
import { Button } from '../components/ui';
import { useTicketPreview } from '../hooks/useTickets';
import { ticketHref } from '../helper/format';
import { dueLabel, linesLabel } from './format';

interface ItemListProps {
  items: MeetingActionItem[];
  participants: MeetingParticipant[];
  ownerName: (p: MeetingParticipant) => string;
  selectedId: string | null;
  onSelect: (item: MeetingActionItem) => void;
  canEdit: boolean;
  /** Project the meeting belongs to; group meetings look the ticket up. */
  projectId: string | null;
  onPromote?: (item: MeetingActionItem) => void;
  onDismiss?: (item: MeetingActionItem) => void;
  onRestore?: (item: MeetingActionItem) => void;
  /** Items with a dismiss/restore in flight. */
  busyItemIds?: ReadonlySet<string>;
}

/**
 * Action items or decisions. The item text is the select control: it
 * highlights and scrolls to the cited transcript lines. Item actions sit
 * beside it (never nested inside a button).
 */
export function ItemList({
  items,
  participants,
  ownerName,
  selectedId,
  onSelect,
  canEdit,
  projectId,
  onPromote,
  onDismiss,
  onRestore,
  busyItemIds,
}: ItemListProps) {
  const owners = new Map(participants.map((p) => [p.id, p]));
  return (
    <ul className="space-y-2">
      {items.map((item) => {
        const owner = item.owner_participant_id ? owners.get(item.owner_participant_id) : undefined;
        const selected = selectedId === item.id;
        const dismissed = item.status === 'dismissed';
        const busy = !!busyItemIds?.has(item.id);
        const range = { start: item.line_start, end: item.line_end };
        return (
          <li
            key={item.id}
            data-item-id={item.id}
            className={`rounded-lg border transition-colors ${
              selected
                ? 'border-[color-mix(in_oklch,var(--brand)_45%,transparent)] bg-[color-mix(in_oklch,var(--brand)_6%,transparent)]'
                : 'border-border bg-bg-primary hover:border-border-strong'
            }`}
          >
            <button
              type="button"
              onClick={() => onSelect(item)}
              aria-pressed={selected}
              className="block w-full text-left px-3 pt-2.5 pb-2 rounded-lg outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] cursor-pointer"
            >
              <span
                className={`block text-sm leading-snug ${
                  dismissed ? 'text-text-tertiary line-through decoration-text-tertiary/50' : 'text-text-primary'
                }`}
              >
                {item.description}
              </span>
              <span className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-tertiary">
                {owner && (
                  <span className="inline-flex items-center gap-1 min-w-0">
                    <span className="sr-only">Owner: </span>
                    <span className="truncate text-text-secondary">{ownerName(owner)}</span>
                  </span>
                )}
                {item.due_date && (
                  <>
                    {owner && <span aria-hidden="true">·</span>}
                    <span>Due {dueLabel(item.due_date)}</span>
                  </>
                )}
                {(owner || item.due_date) && <span aria-hidden="true">·</span>}
                <span className={`font-mono ${selected ? 'text-brand' : ''}`}>{linesLabel(range)}</span>
              </span>
            </button>
            <ItemFooter
              item={item}
              canEdit={canEdit}
              projectId={projectId}
              busy={busy}
              onPromote={onPromote}
              onDismiss={onDismiss}
              onRestore={onRestore}
            />
          </li>
        );
      })}
    </ul>
  );
}

function ItemFooter({
  item,
  canEdit,
  projectId,
  busy,
  onPromote,
  onDismiss,
  onRestore,
}: {
  item: MeetingActionItem;
  canEdit: boolean;
  projectId: string | null;
  busy: boolean;
  onPromote?: (item: MeetingActionItem) => void;
  onDismiss?: (item: MeetingActionItem) => void;
  onRestore?: (item: MeetingActionItem) => void;
}) {
  if (item.status === 'promoted' && item.promoted_ticket_id) {
    return (
      <div className="flex items-center gap-1.5 border-t border-border px-3 py-1.5 text-xs text-text-tertiary">
        <CheckIcon />
        <span>Ticket</span>
        <PromotedTicketLink ticketId={item.promoted_ticket_id} projectId={projectId} />
      </div>
    );
  }
  if (item.status === 'dismissed') {
    return (
      <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-1 text-xs text-text-tertiary">
        <span>Dismissed</span>
        {canEdit && onRestore && (
          <Button variant="ghost" size="sm" onClick={() => onRestore(item)} disabled={busy} aria-label={`Restore: ${item.description}`}>
            Restore
          </Button>
        )}
      </div>
    );
  }
  // Any pending item can be dismissed; only action items become tickets.
  if (!canEdit) return null;
  return (
    <div className="flex items-center justify-end gap-1 border-t border-border px-2 py-1">
      {onDismiss && (
        <Button variant="ghost" size="sm" onClick={() => onDismiss(item)} disabled={busy} aria-label={`Dismiss: ${item.description}`}>
          Dismiss
        </Button>
      )}
      {onPromote && item.kind === 'action' && (
        <Button variant="secondary" size="sm" onClick={() => onPromote(item)} disabled={busy} aria-label={`Create ticket: ${item.description}`}>
          Create ticket
        </Button>
      )}
    </div>
  );
}

const LINK =
  'font-mono text-brand underline decoration-brand/40 underline-offset-2 hover:decoration-brand rounded-sm outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]';

/** A link to the ticket a promoted item became. Group meetings look up its project. */
export function PromotedTicketLink({ ticketId, projectId }: { ticketId: string; projectId: string | null }) {
  const preview = useTicketPreview(projectId ? null : ticketId);
  const target = projectId ?? preview.data?.project_id ?? null;
  if (!target) return <span className="font-mono text-text-secondary">{ticketId}</span>;
  return (
    <Link to={ticketHref(target, ticketId)} className={LINK}>
      {ticketId}
    </Link>
  );
}

function CheckIcon() {
  return (
    <svg className="h-3.5 w-3.5 shrink-0 text-success" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}
