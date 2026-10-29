import { useMemo } from 'react';
import { ApiError, type MeetingParticipant, type MeetingScope, type Persona } from '../api/client';
import { useOrgMembers } from '../org/useOrgMembers';
import { useOrgPersonas, usePersonas } from '../hooks/usePersonas';
import { useAssignableUsers } from '../hooks/useTickets';
import { personName } from '../projects/tickets/assignee';

export interface MeetingPerson {
  id: string;
  name: string;
  email: string;
}

export interface MeetingPeople {
  people: MeetingPerson[];
  personas: Persona[];
  loading: boolean;
  /** Why people aren't listed (a reader's 403, a failed load); personas still are. */
  peopleNote?: string;
  nameOf: (userId: string) => string | null;
}

/**
 * Who a meeting's participants can be matched to — the same set the server
 * accepts: for a project, the ticket assignee set (assignable people +
 * personas visible in the project); for a group, the org's members and org
 * personas.
 */
export function useMeetingPeople(scope: MeetingScope, orgId: string | null | undefined): MeetingPeople {
  const projectId = scope.kind === 'project' ? scope.id : undefined;
  const groupOrgId = scope.kind === 'group' ? orgId ?? undefined : undefined;
  const assignable = useAssignableUsers(projectId);
  const projectPersonas = usePersonas(projectId);
  const members = useOrgMembers(groupOrgId);
  const orgPersonas = useOrgPersonas(groupOrgId);

  const usersData = assignable.data;
  const membersData = members.data;
  const people = useMemo<MeetingPerson[]>(() => {
    if (projectId) {
      return (usersData ?? []).map((u) => ({ id: u.id, name: personName(u), email: u.email }));
    }
    return (membersData?.members ?? []).map((m) => ({
      id: m.user_id,
      name: personName({ display_name: m.display_name, email: m.email }),
      email: m.email,
    }));
  }, [projectId, usersData, membersData]);

  const personasData = projectId ? projectPersonas.data : orgPersonas.data;
  const personas = useMemo(() => (personasData ?? []).filter((p) => p.is_active), [personasData]);

  const peopleError = projectId ? assignable.error : members.error;
  const peopleNote = peopleError
    ? peopleError instanceof ApiError && peopleError.status === 403
      ? projectId
        ? 'People aren’t listed: you can view this project but not assign people in it.'
        : 'People aren’t listed: you can view this group but not see its organization’s members.'
      : 'People couldn’t be loaded. Personas are still available.'
    : undefined;

  const byId = useMemo(() => new Map(people.map((p) => [p.id, p.name])), [people]);
  const nameOf = useMemo(() => (userId: string) => byId.get(userId) ?? null, [byId]);

  return {
    people,
    personas,
    loading: projectId
      ? assignable.isLoading || projectPersonas.isLoading
      : members.isLoading || orgPersonas.isLoading,
    peopleNote,
    nameOf,
  };
}

/** How a participant shows: who they were matched to, else the raw name. */
export function participantName(p: MeetingParticipant, people: Pick<MeetingPeople, 'nameOf'>): string {
  if (p.persona_name) return p.persona_name;
  if (p.user_id) return people.nameOf(p.user_id) ?? p.name_raw;
  return p.name_raw;
}
