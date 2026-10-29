import { useMemo, useState } from 'react';
import type { MeetingParticipant } from '../api/client';
import { Button, Combobox, type ComboboxOption } from '../components/ui';
import { useToast } from '../hooks/useToast';
import { useResolveParticipant } from '../hooks/useMeetings';
import { PersonAvatar, PersonaGlyph } from '../projects/tickets/AssigneeLabel';
import { RESOLUTION_LABEL, isResolved, meetingActionError } from './format';
import { participantName, type MeetingPeople } from './people';

interface ParticipantsProps {
  meetingId: string;
  participants: MeetingParticipant[];
  people: MeetingPeople;
  canEdit: boolean;
}

/**
 * Who was in the meeting. Matched participants show who they were matched to
 * and how; unmatched ones offer a picker (editors) over the same people and
 * personas the server accepts. Naming someone never grants them access —
 * matching only labels the row.
 */
export function Participants({ meetingId, participants, people, canEdit }: ParticipantsProps) {
  const sorted = useMemo(
    () => [...participants].sort((a, b) => Number(isResolved(a)) - Number(isResolved(b)) || a.name_raw.localeCompare(b.name_raw)),
    [participants],
  );
  if (participants.length === 0) {
    return <p className="text-sm text-text-tertiary">No participants yet. Extraction lists who spoke.</p>;
  }
  return (
    <ul className="space-y-1">
      {sorted.map((p) => (
        <ParticipantRow key={p.id} meetingId={meetingId} participant={p} people={people} canEdit={canEdit} />
      ))}
    </ul>
  );
}

function ParticipantRow({
  meetingId,
  participant: p,
  people,
  canEdit,
}: {
  meetingId: string;
  participant: MeetingParticipant;
  people: MeetingPeople;
  canEdit: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const resolved = isResolved(p);
  const name = participantName(p, people);
  const raw = p.name_raw && p.name_raw !== name ? p.name_raw : null;

  return (
    <li className="rounded-lg px-2 py-1.5 -mx-2 hover:bg-bg-elevated/60">
      <div className="flex items-center gap-2.5">
        {p.persona_name ? (
          <PersonaBadge />
        ) : resolved ? (
          <PersonAvatar name={name} seed={p.email_raw ?? p.user_id ?? p.name_raw} className="!h-6 !w-6 !text-xs" />
        ) : (
          <span
            aria-hidden="true"
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-dashed border-border-strong text-xs text-text-tertiary"
          >
            ?
          </span>
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-text-primary">{name}</p>
          <p className="truncate text-xs text-text-tertiary">
            {resolved ? RESOLUTION_LABEL[p.resolution_method] ?? 'Matched' : 'Not matched to anyone'}
            {raw ? ` · “${raw}”` : ''}
          </p>
        </div>
        {canEdit && !editing && (
          <Button
            variant={resolved ? 'ghost' : 'secondary'}
            size="sm"
            onClick={() => setEditing(true)}
            aria-label={resolved ? `Change match for ${p.name_raw}` : `Match ${p.name_raw}`}
          >
            {resolved ? 'Change' : 'Match'}
          </Button>
        )}
      </div>
      {editing && (
        <ParticipantPicker
          meetingId={meetingId}
          participant={p}
          people={people}
          onDone={() => setEditing(false)}
        />
      )}
    </li>
  );
}

const PERSON = 'user:';
const PERSONA = 'persona:';

export function ParticipantPicker({
  meetingId,
  participant,
  people,
  onDone,
}: {
  meetingId: string;
  participant: MeetingParticipant;
  people: MeetingPeople;
  onDone: () => void;
}) {
  const resolve = useResolveParticipant(meetingId);
  const { addToast } = useToast();
  const [error, setError] = useState<string | undefined>();

  const options = useMemo<ComboboxOption[]>(
    () => [
      ...[...people.people]
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((u) => ({
          value: `${PERSON}${u.id}`,
          label: u.name,
          description: u.name === u.email ? undefined : u.email,
          icon: <PersonAvatar name={u.name} seed={u.email} />,
          group: 'People',
          searchText: `${u.name} ${u.email}`,
        })),
      ...[...people.personas]
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((p) => ({
          value: `${PERSONA}${p.name}`,
          label: p.name,
          description: p.role || undefined,
          icon: <PersonaGlyph />,
          group: 'Personas',
          searchText: `${p.name} ${p.role ?? ''}`,
        })),
    ],
    [people.people, people.personas],
  );

  const current = participant.persona_name
    ? `${PERSONA}${participant.persona_name}`
    : participant.user_id
      ? `${PERSON}${participant.user_id}`
      : '';

  function pick(value: string) {
    if (!value || value === current) {
      onDone();
      return;
    }
    const body = value.startsWith(PERSONA)
      ? { persona_name: value.slice(PERSONA.length) }
      : { user_id: value.slice(PERSON.length) };
    setError(undefined);
    resolve.mutate(
      { participantId: participant.id, body },
      {
        onSuccess: () => {
          addToast('success', `Matched ${participant.name_raw}`);
          onDone();
        },
        onError: (err) => setError(meetingActionError(err)),
      },
    );
  }

  return (
    <div className="mt-2 flex items-start gap-2 pl-8">
      <div className="min-w-0 flex-1">
        <Combobox
          title={`Match “${participant.name_raw}” to`}
          value={current}
          onValueChange={pick}
          options={options}
          placeholder="Choose a person or persona"
          searchPlaceholder="Search people and personas"
          loading={people.loading}
          disabled={resolve.isPending}
          emptyMessage={people.peopleNote ?? 'No people or personas to match'}
          footer={people.peopleNote}
          error={error}
        />
      </div>
      <Button variant="ghost" size="sm" className="mt-6" onClick={onDone}>
        Cancel
      </Button>
    </div>
  );
}

/** A persona's glyph at the participant-row size. */
function PersonaBadge() {
  return (
    <span aria-hidden="true" className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-surface-hover text-text-tertiary">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="8" width="16" height="12" rx="2" />
        <path d="M12 8V4" />
        <path d="M9 13h.01M15 13h.01" />
      </svg>
    </span>
  );
}
