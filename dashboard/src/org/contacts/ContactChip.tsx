import type { ContactRef } from '../../api/client';

/* ── External contact display ──
 *
 * A contact is a person the org attributes work to who never logs in. It
 * reads as a person (a dashed-ring avatar, so it never passes for a member)
 * with a quiet "external" tag, or "member" once it was linked to a member by
 * verified email. */

export function ContactAvatar({ name, className = '' }: { name: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-dashed border-border-strong bg-bg-tertiary text-2xs font-semibold leading-none text-text-secondary ${className}`}
    >
      {(name.trim().charAt(0) || '?').toUpperCase()}
    </span>
  );
}

/** The small "external" tag; "member" when the contact has since joined. */
export function ExternalBadge({ linked = false, archived = false }: { linked?: boolean; archived?: boolean }) {
  const label = linked ? 'member' : archived ? 'external · archived' : 'external';
  return (
    <span
      data-testid="external-badge"
      className="inline-flex shrink-0 items-center rounded px-1 py-px text-2xs font-medium leading-tight text-info bg-[color-mix(in_oklch,var(--info)_12%,transparent)]"
    >
      {label}
    </span>
  );
}

/** Avatar + name + tag, at the caller's type size. */
export function ContactName({
  contact,
  className = '',
  nameClassName = '',
}: {
  contact: Pick<ContactRef, 'display_name' | 'role' | 'status' | 'linked_user_id'>;
  className?: string;
  nameClassName?: string;
}) {
  return (
    <span className={`inline-flex min-w-0 items-center gap-1 align-middle ${className}`} data-assignee="contact">
      <ContactAvatar name={contact.display_name} />
      <span className={`truncate ${nameClassName}`} title={contact.role ?? undefined}>
        {contact.display_name}
      </span>
      <ExternalBadge linked={!!contact.linked_user_id} archived={contact.status === 'archived'} />
    </span>
  );
}
