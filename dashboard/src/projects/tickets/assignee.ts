import { ApiError, type AssignableUser, type Persona, type Ticket, type TicketAssignedUser, type TicketAssigneeFields } from '../../api/client';

/**
 * A ticket is assigned to a persona (an agent role, `assigned_to`) OR to a
 * person (`assigned_user_id`), never both. A person is only notified about a
 * persona assignment when that persona represents them.
 */
export type Assignee =
  | { kind: 'none' }
  | { kind: 'person'; userId: string }
  | { kind: 'persona'; name: string };

export const UNASSIGNED: Assignee = { kind: 'none' };

const PERSON_PREFIX = 'user:';
const PERSONA_PREFIX = 'persona:';

/** Option value for the picker: '' | 'user:<id>' | 'persona:<name>'. */
export function encodeAssignee(a: Assignee): string {
  if (a.kind === 'person') return `${PERSON_PREFIX}${a.userId}`;
  if (a.kind === 'persona') return `${PERSONA_PREFIX}${a.name}`;
  return '';
}

export function decodeAssignee(value: string): Assignee {
  if (value.startsWith(PERSON_PREFIX) && value.length > PERSON_PREFIX.length) {
    return { kind: 'person', userId: value.slice(PERSON_PREFIX.length) };
  }
  if (value.startsWith(PERSONA_PREFIX) && value.length > PERSONA_PREFIX.length) {
    return { kind: 'persona', name: value.slice(PERSONA_PREFIX.length) };
  }
  return UNASSIGNED;
}

/** The current assignee of a ticket. A person wins if a server ever sent both. */
export function ticketAssignee(t: Pick<Ticket, 'assigned_to' | 'assigned_user_id'>): Assignee {
  if (t.assigned_user_id) return { kind: 'person', userId: t.assigned_user_id };
  if (t.assigned_to) return { kind: 'persona', name: t.assigned_to };
  return UNASSIGNED;
}

export function sameAssignee(a: Assignee, b: Assignee): boolean {
  return encodeAssignee(a) === encodeAssignee(b);
}

/**
 * Request fields for create/update. Both fields are always sent so the
 * request states the whole assignee: the one that is set, and an explicit
 * null for the other (never both non-null — the server answers 422
 * `assignee_conflict`).
 */
export function assigneePayload(a: Assignee): Required<TicketAssigneeFields> {
  if (a.kind === 'person') return { assigned_user_id: a.userId, assigned_to: null };
  if (a.kind === 'persona') return { assigned_to: a.name, assigned_user_id: null };
  return { assigned_to: null, assigned_user_id: null };
}

/** Display name for a person: their display name, else their email. */
export function personName(u: Pick<AssignableUser, 'display_name' | 'email'>): string {
  return u.display_name?.trim() || u.email;
}

/** Name for a ticket's person assignee, from the embedded user when present. */
export function ticketPersonName(t: Pick<Ticket, 'assigned_user' | 'assigned_user_id'>): string {
  if (t.assigned_user) return personName(t.assigned_user);
  return t.assigned_user_id ? `user ${t.assigned_user_id.slice(0, 8)}` : '';
}

/** What assigning to `a` means for notifications, shown under the picker. */
export type AssigneeNote =
  | { kind: 'notifies'; name: string; you: boolean }
  | { kind: 'unrepresented'; persona: string; orgId: string | null; orgPersona: boolean }
  | null;

export function assigneeNote(
  a: Assignee,
  {
    personas,
    users,
    meId,
  }: {
    personas: Persona[] | undefined;
    /** Assignable users, plus any person already on the ticket. */
    users: Array<AssignableUser | TicketAssignedUser> | undefined;
    meId: string | null | undefined;
  },
): AssigneeNote {
  const nameOf = (userId: string, fallback: string) => {
    const u = users?.find((x) => x.id === userId);
    return u ? personName(u) : fallback;
  };
  if (a.kind === 'person') {
    return { kind: 'notifies', name: nameOf(a.userId, 'this person'), you: !!meId && a.userId === meId };
  }
  if (a.kind === 'persona') {
    const p = personas?.find((x) => x.name === a.name);
    // Unknown persona (list still loading, or a persona since removed):
    // say nothing rather than warn on a guess.
    if (!p) return null;
    if (p.represented_user_id) {
      return {
        kind: 'notifies',
        name: nameOf(p.represented_user_id, 'the person it represents'),
        you: !!meId && p.represented_user_id === meId,
      };
    }
    const orgPersona = p.scope === 'org' || (!p.project_id && !!p.org_id);
    return { kind: 'unrepresented', persona: p.name, orgId: p.org_id ?? null, orgPersona };
  }
  return null;
}

/** Map assignee error codes from create/update to a sentence. */
export function assigneeErrorMessage(code: string | undefined): string | null {
  switch (code) {
    case 'assignee_not_eligible':
      return "That person can't be assigned tickets in this project.";
    case 'assignee_conflict':
      return 'A ticket can be assigned to a person or a persona, not both.';
    default:
      return null;
  }
}

/** The friendly message for an assignee error thrown by create/update, or null. */
export function assigneeErrorFrom(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const details = err.details as { error?: unknown } | undefined;
  const code = details && typeof details.error === 'string' ? details.error : err.code;
  return assigneeErrorMessage(code);
}
