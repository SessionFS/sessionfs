import { useMemo, type ReactNode } from 'react';
import { Link, useInRouterContext } from 'react-router-dom';
import { ApiError, type TicketAssignedUser } from '../../api/client';
import { Combobox, type ComboboxOption } from '../../components/ui';
import { useMe } from '../../hooks/useMe';
import { usePersonas } from '../../hooks/usePersonas';
import { useAssignableUsers } from '../../hooks/useTickets';
import {
  assigneeNote,
  decodeAssignee,
  encodeAssignee,
  personName,
  type Assignee,
  type AssigneeNote,
} from './assignee';
import { PersonAvatar, PersonaGlyph } from './AssigneeLabel';

/* ── Assignee picker ──
 *
 * One searchable list: "Unassigned", then People (who get notified), then
 * Personas (agent roles). Built on ui/Combobox, so the search box keeps
 * focus and the active row is exposed with aria-activedescendant. The note
 * under the field says who the choice notifies — or warns, quietly, when
 * the chosen persona represents no one. */

interface AssigneePickerProps {
  projectId: string;
  value: Assignee;
  onChange: (next: Assignee) => void;
  id?: string;
  title?: string;
  /** The person already on the ticket, so its name shows even when they are
   *  no longer in the assignable list. */
  currentUser?: TicketAssignedUser | null;
  disabled?: boolean;
  error?: string;
}

export function AssigneePicker({
  projectId,
  value,
  onChange,
  id,
  title = 'Assignee',
  currentUser,
  disabled,
  error,
}: AssigneePickerProps) {
  const { data: users, isLoading: usersLoading, error: usersError } = useAssignableUsers(projectId);
  // Readers whose only access is a captured session get 403 here: offer
  // personas only and say why people are missing.
  const peopleNote = usersError
    ? usersError instanceof ApiError && usersError.status === 403
      ? 'People aren’t listed: you can view this project but not assign people in it.'
      : 'People couldn’t be loaded. Personas are still available.'
    : undefined;
  const { data: personas, isLoading: personasLoading } = usePersonas(projectId);
  const { data: me } = useMe();
  const meId: string | null = me?.user_id ?? null;

  const options = useMemo<ComboboxOption[]>(() => {
    const people = [...(users ?? [])].sort((a, b) => {
      // You first, then by name.
      if (a.id === meId) return -1;
      if (b.id === meId) return 1;
      return personName(a).localeCompare(personName(b));
    });
    const roles = (personas ?? [])
      .filter((p) => p.is_active)
      .sort((a, b) => a.name.localeCompare(b.name));
    return [
      { value: '', label: 'Unassigned', searchText: 'unassigned none' },
      ...people.map((u) => {
        const name = personName(u);
        return {
          value: encodeAssignee({ kind: 'person', userId: u.id }),
          label: name,
          // The email is the label when there is no display name.
          description: name === u.email ? undefined : u.email,
          meta: u.id === meId ? 'You' : undefined,
          icon: <PersonAvatar name={name} seed={u.email} />,
          group: 'People',
          searchText: `${name} ${u.email}${u.id === meId ? ' me you' : ''}`,
        };
      }),
      ...roles.map((p) => ({
        value: encodeAssignee({ kind: 'persona', name: p.name }),
        label: p.name,
        description: p.role || undefined,
        icon: <PersonaGlyph />,
        group: 'Personas',
        searchText: `${p.name} ${p.role}`,
      })),
    ];
  }, [users, personas, meId]);

  // Trigger text when the current assignee isn't in the list (a person no
  // longer assignable, an inactive or removed persona).
  const selectedLabel =
    value.kind === 'person'
      ? currentUser && currentUser.id === value.userId
        ? personName(currentUser)
        : `user ${value.userId.slice(0, 8)}`
      : value.kind === 'persona'
        ? value.name
        : undefined;

  const knownUsers = useMemo(
    () => [...(users ?? []), ...(currentUser ? [currentUser] : [])],
    [users, currentUser],
  );
  const note = assigneeNote(value, { personas, users: knownUsers, meId });

  return (
    <Combobox
      id={id}
      title={title}
      value={encodeAssignee(value)}
      onValueChange={(v) => onChange(decodeAssignee(v))}
      options={options}
      selectedLabel={selectedLabel}
      placeholder="Unassigned"
      loading={usersLoading || personasLoading}
      emptyMessage={peopleNote ?? 'No people or personas yet'}
      footer={peopleNote}
      disabled={disabled}
      error={error}
      hint={note ? <AssigneeNoteText note={note} /> : undefined}
    />
  );
}

/** The line under the picker. Kept to one quiet sentence. */
export function AssigneeNoteText({ note }: { note: NonNullable<AssigneeNote> }) {
  if (note.kind === 'notifies') {
    return (
      <span data-testid="assignee-note" className="inline-flex items-center gap-1">
        <BellIcon />
        {note.you ? 'Notifies you' : `Notifies ${note.name}`}
      </span>
    );
  }
  const settings = note.orgPersona ? (
    <SettingsLink to={`/settings/organization?tab=personas${note.orgId ? `&org=${encodeURIComponent(note.orgId)}` : ''}`}>
      persona settings
    </SettingsLink>
  ) : null;
  return (
    <span data-testid="assignee-note" data-tone="warning" className="inline-flex items-start gap-1">
      <WarningIcon />
      <span>
        <span className="text-text-secondary">No one is notified:</span> {note.persona} doesn’t represent a person.{' '}
        {settings ? (
          <>Assign a person, or set who this persona represents in {settings}.</>
        ) : (
          <>Assign a person, or use an org persona that represents someone.</>
        )}
      </span>
    </span>
  );
}

function SettingsLink({ to, children }: { to: string; children: ReactNode }) {
  const inRouter = useInRouterContext();
  const className = 'text-brand underline-offset-2 hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm';
  return inRouter ? (
    <Link to={to} className={className}>
      {children}
    </Link>
  ) : (
    <a href={to} className={className}>
      {children}
    </a>
  );
}

function WarningIcon() {
  return (
    <svg className="mt-px h-3 w-3 shrink-0 text-warning" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      <path d="M12 9v4M12 17h.01" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg className="h-3 w-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.7 21a2 2 0 0 1-3.4 0" />
    </svg>
  );
}
