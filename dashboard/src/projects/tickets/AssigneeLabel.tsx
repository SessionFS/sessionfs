import type { Ticket } from '../../api/client';
import { getAvatarColor } from '../../utils/avatar';
import { ticketPersonName } from './assignee';

/* ── Assignee display ──
 *
 * A person reads as a person (initial avatar + name); a persona reads as it
 * always has (its name, a mono chip on board cards). Both sit on the same
 * baseline at the caller's type size so rows don't shift between them. */

export function PersonAvatar({ name, seed, className = '' }: { name: string; seed: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-2xs font-semibold leading-none ${getAvatarColor(seed)} ${className}`}
    >
      {(name.trim().charAt(0) || '?').toUpperCase()}
    </span>
  );
}

/** A small "agent role" glyph for personas in the picker. */
export function PersonaGlyph() {
  return (
    <span
      aria-hidden="true"
      className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded bg-surface-hover text-text-tertiary"
    >
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="8" width="16" height="12" rx="2" />
        <path d="M12 8V4" />
        <path d="M9 13h.01M15 13h.01" />
      </svg>
    </span>
  );
}

/**
 * The assignee of a ticket, or nothing when unassigned.
 * `variant="chip"` (board cards) shows a persona as a mono chip;
 * `variant="text"` (list rows, detail) shows it as plain text.
 */
export function AssigneeLabel({
  ticket: t,
  variant = 'text',
  emphasis = false,
}: {
  ticket: Pick<Ticket, 'assigned_to' | 'assigned_user' | 'assigned_user_id'>;
  variant?: 'text' | 'chip';
  /** Primary-coloured, medium-weight name (the detail header). */
  emphasis?: boolean;
}) {
  const nameClass = emphasis ? 'font-medium text-text-primary' : '';
  if (t.assigned_user_id) {
    const name = ticketPersonName(t);
    return (
      <span className="inline-flex min-w-0 items-center gap-1 align-middle" data-assignee="person">
        <PersonAvatar name={name} seed={t.assigned_user?.email ?? t.assigned_user_id} />
        <span className={`truncate ${nameClass}`} title={t.assigned_user?.email ?? undefined}>
          {name}
        </span>
      </span>
    );
  }
  if (t.assigned_to) {
    return variant === 'chip' ? (
      <span className="text-mono-chip text-2xs" data-assignee="persona">
        {t.assigned_to}
      </span>
    ) : (
      <span className={nameClass} data-assignee="persona">
        {t.assigned_to}
      </span>
    );
  }
  return null;
}
