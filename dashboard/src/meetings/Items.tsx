import type React from 'react';
import { ExternalBadge } from '../org/contacts/ContactChip';
import { Link } from 'react-router-dom';
import type { MeetingActionItem, MeetingItemTicketRef, MeetingParticipant } from '../api/client';
import { Badge, Button } from '../components/ui';
import { useTicketPreview } from '../hooks/useTickets';
import { ticketHref } from '../helper/format';
import { STATUS_VARIANT, formatStatusLabel } from '../projects/tickets/constants';
import { messagesLabel } from './chat';
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
  onRemind?: (item: MeetingActionItem) => void;
  onDismiss?: (item: MeetingActionItem) => void;
  onRestore?: (item: MeetingActionItem) => void;
  /** Link the item to its suggested ticket (one click). */
  onLinkSuggested?: (item: MeetingActionItem) => void;
  /** "Not this one": hide the suggestion. */
  onDismissSuggestion?: (item: MeetingActionItem) => void;
  /** Open the ticket picker for this item. */
  onPickTicket?: (item: MeetingActionItem) => void;
  onUnlink?: (item: MeetingActionItem) => void;
  /** Items with a write in flight. */
  busyItemIds?: ReadonlySet<string>;
  /** Items cite chat messages (M12-15) instead of transcript lines. */
  chat?: boolean;
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
  onRemind,
  onDismiss,
  onRestore,
  onLinkSuggested,
  onDismissSuggestion,
  onPickTicket,
  onUnlink,
  busyItemIds,
  chat = false,
}: ItemListProps) {
  const owners = new Map(participants.map((p) => [p.id, p]));
  return (
    <ul className="space-y-2">
      {items.map((item) => {
        const owner = item.owner_participant_id ? owners.get(item.owner_participant_id) : undefined;
        // An action with no matched owner: say so, so someone assigns it on
        // Create ticket (decisions have no owner to find).
        const unknownOwner = !owner && item.kind === 'action' && item.status === 'pending';
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
                    {!owner.user_id && !owner.persona_name && owner.contact && (
                      <ExternalBadge linked={!!owner.contact.linked_user_id} archived={owner.contact.status === 'archived'} />
                    )}
                  </span>
                )}
                {unknownOwner && (
                  <span className="inline-flex items-center gap-1 min-w-0" title={item.owner_hint ? `The ${chat ? 'thread' : 'transcript'} named “${item.owner_hint}”, who isn’t matched to anyone` : undefined}>
                    <span className="text-warning">Owner unknown</span>
                    {item.owner_hint && <span className="truncate">(“{item.owner_hint}”)</span>}
                  </span>
                )}
                {item.due_date && (
                  <>
                    {(owner || unknownOwner) && <span aria-hidden="true">·</span>}
                    <span>Due {dueLabel(item.due_date)}</span>
                  </>
                )}
                {(owner || unknownOwner || item.due_date) && <span aria-hidden="true">·</span>}
                <span className={`font-mono ${selected ? 'text-brand' : ''}`}>{chat ? messagesLabel(range) : linesLabel(range)}</span>
              </span>
            </button>
            <ItemFooter
              item={item}
              canEdit={canEdit}
              projectId={projectId}
              busy={busy}
              onPromote={onPromote}
              onRemind={onRemind}
              onDismiss={onDismiss}
              onRestore={onRestore}
              onLinkSuggested={onLinkSuggested}
              onDismissSuggestion={onDismissSuggestion}
              onPickTicket={onPickTicket}
              onUnlink={onUnlink}
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
  onRemind,
  onDismiss,
  onRestore,
  onLinkSuggested,
  onDismissSuggestion,
  onPickTicket,
  onUnlink,
}: {
  item: MeetingActionItem;
  canEdit: boolean;
  projectId: string | null;
  busy: boolean;
  onPromote?: (item: MeetingActionItem) => void;
  onRemind?: (item: MeetingActionItem) => void;
  onDismiss?: (item: MeetingActionItem) => void;
  onRestore?: (item: MeetingActionItem) => void;
  onLinkSuggested?: (item: MeetingActionItem) => void;
  onDismissSuggestion?: (item: MeetingActionItem) => void;
  onPickTicket?: (item: MeetingActionItem) => void;
  onUnlink?: (item: MeetingActionItem) => void;
}) {
  if (item.status === 'linked') {
    const ticket = item.linked_ticket;
    return (
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 border-t border-border px-3 py-1.5 text-xs text-text-tertiary">
        <span className="flex min-w-0 items-center gap-1.5">
          <LinkIcon />
          <span className="shrink-0">Linked to</span>
          {ticket ? (
            <TicketRefInline ticket={ticket} />
          ) : (
            // Linked to a ticket the caller can't read: never name it.
            <span>a ticket you can’t see</span>
          )}
        </span>
        {canEdit && onUnlink && (
          <Button variant="ghost" size="sm" onClick={() => onUnlink(item)} disabled={busy} aria-label={`Unlink: ${item.description}`}>
            Unlink
          </Button>
        )}
      </div>
    );
  }
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
  const suggestion = item.status === 'pending' ? item.suggested_ticket : null;
  // Any pending item can be dismissed or linked; only action items become tickets.
  if (!canEdit) {
    return suggestion ? <SuggestionRow ticket={suggestion} /> : null;
  }
  return (
    <>
    {suggestion && (
      <SuggestionRow ticket={suggestion}>
        {onDismissSuggestion && (
          <Button variant="ghost" size="sm" onClick={() => onDismissSuggestion(item)} disabled={busy} aria-label={`Not this one: ${suggestion.id} for ${item.description}`}>
            Not this one
          </Button>
        )}
        {onPromote && item.kind === 'action' && (
          <Button variant="ghost" size="sm" onClick={() => onPromote(item)} disabled={busy} aria-label={`Create new ticket: ${item.description}`}>
            Create new
          </Button>
        )}
        {onLinkSuggested && (
          <Button variant="secondary" size="sm" onClick={() => onLinkSuggested(item)} disabled={busy} aria-label={`Link to ${suggestion.id}: ${item.description}`}>
            Link
          </Button>
        )}
      </SuggestionRow>
    )}
    <div className="flex flex-wrap items-center justify-end gap-1 border-t border-border px-2 py-1">
      {onDismiss && (
        <Button variant="ghost" size="sm" onClick={() => onDismiss(item)} disabled={busy} aria-label={`Dismiss: ${item.description}`}>
          Dismiss
        </Button>
      )}
      {onRemind && (
        <Button variant="ghost" size="sm" onClick={() => onRemind(item)} disabled={busy} aria-label={`Add reminder: ${item.description}`}>
          Add reminder
        </Button>
      )}
      {onPickTicket && (
        <Button variant="ghost" size="sm" onClick={() => onPickTicket(item)} disabled={busy} aria-label={`Link to ticket: ${item.description}`}>
          Link to ticket…
        </Button>
      )}
      {onPromote && item.kind === 'action' && !suggestion && (
        <Button variant="secondary" size="sm" onClick={() => onPromote(item)} disabled={busy} aria-label={`Create ticket: ${item.description}`}>
          Create ticket
        </Button>
      )}
    </div>
    </>
  );
}

/**
 * "Looks like tk_… · title · status": an existing ticket the extraction
 * thinks this item is. A suggestion only; the actions (children) decide.
 */
function SuggestionRow({ ticket, children }: { ticket: MeetingItemTicketRef; children?: React.ReactNode }) {
  return (
    <div className="border-t border-dashed border-border bg-bg-sunken px-3 py-2">
      <p className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-text-secondary">
        <LinkIcon />
        <span className="shrink-0">Looks like</span>
        <TicketRefInline ticket={ticket} />
      </p>
      {children && <div className="mt-1.5 flex flex-wrap items-center justify-end gap-1">{children}</div>}
    </div>
  );
}

/** `tk_… · title · status` with the id linking to the ticket. */
function TicketRefInline({ ticket }: { ticket: MeetingItemTicketRef }) {
  return (
    <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-0.5">
      <Link to={ticketHref(ticket.project_id, ticket.id)} className={LINK} title={ticket.title}>
        {ticket.id}
      </Link>
      <span aria-hidden="true">·</span>
      {/* Wraps onto its own line in a narrow column rather than truncating to a word. */}
      <span className="min-w-0 break-words line-clamp-2 text-text-primary" title={ticket.title}>
        {ticket.title}
      </span>
      <span className="shrink-0">
        <Badge variant={STATUS_VARIANT[ticket.status] ?? 'default'} label={formatStatusLabel(ticket.status)} tint />
      </span>
    </span>
  );
}

function LinkIcon() {
  return (
    <svg className="h-3.5 w-3.5 shrink-0 text-text-tertiary" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
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
