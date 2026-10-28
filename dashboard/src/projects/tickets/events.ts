import type { TicketEvent, TicketEventActor } from '../../api/client';
import { formatStatusLabel } from './constants';
import { formatDueDate } from './schedule';

/* ── Ticket event log → readable sentences ──
 *
 * Event values are bounded server-side: free text arrives as
 * `{changed, length}`, reference lists as `{count, items}`, assignments as
 * `{persona}` / `{user_id}`, Jira rows as `{issue_key}`. Every renderer here
 * reads only those known shapes and falls back to plain words, so a value
 * the dashboard doesn't recognise never shows up as raw JSON. */

export interface EventSentence {
  /** Who did it: a person, a service key, Jira or SessionFS. */
  actor: string;
  /** The rest of the sentence, lower-case start, no trailing period. */
  text: string;
}

/** user id → display name, for assignment events that only carry ids. */
export type UserNames = Map<string, string>;

const FIELD_LABELS: Record<string, string> = {
  title: 'title',
  description: 'description',
  priority: 'priority',
  acceptance_criteria: 'acceptance criteria',
  depends_on: 'dependencies',
  context_refs: 'context references',
  file_refs: 'file references',
  related_sessions: 'related sessions',
  kind: 'kind',
  parent_ticket_id: 'parent issue',
};

export function actorName(actor: TicketEventActor | null | undefined): string {
  if (!actor) return 'Someone';
  const label = actor.label?.trim();
  switch (actor.type) {
    case 'system':
      return 'SessionFS';
    case 'jira':
      return label ? `Jira (${label})` : 'Jira';
    case 'service':
      return label ? `Service key ${label}` : 'A service key';
    default: {
      const name = actor.display_name?.trim();
      const persona = actor.persona?.trim();
      if (name && persona) return `${name} (${persona})`;
      if (name) return name;
      if (persona) return persona;
      return actor.user_id ? 'A teammate' : 'Someone';
    }
  }
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null;
}

function issueKey(e: TicketEvent): string | null {
  return str(obj(e.after)?.issue_key) ?? str(obj(e.before)?.issue_key);
}

/** The person or persona an assignment value names. */
function assigneeText(v: unknown, names: UserNames): string | null {
  const o = obj(v);
  if (!o) return null;
  const persona = str(o.persona);
  if (persona) return `the ${persona} persona`;
  const uid = str(o.user_id);
  if (uid) return names.get(uid) ?? 'a teammate';
  return null;
}

/** A short, safe rendering of a scalar field value, or null. */
function scalar(v: unknown): string | null {
  if (typeof v === 'string') return v.trim() ? v.trim() : null;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return null;
}

function listCount(v: unknown): number | null {
  const o = obj(v);
  if (o && typeof o.count === 'number') return o.count;
  if (Array.isArray(v)) return v.length;
  return null;
}

function fieldEdited(e: TicketEvent): string {
  const field = e.field ?? '';
  const label = FIELD_LABELS[field] ?? field.replace(/_/g, ' ') ?? 'ticket';
  if (field === 'description' || field === 'acceptance_criteria') return `edited the ${label}`;
  if (field === 'priority' || field === 'kind') {
    const from = scalar(e.before);
    const to = scalar(e.after);
    if (from && to) return `changed the ${label} from ${from} to ${to}`;
    if (to) return `set the ${label} to ${to}`;
    return `changed the ${label}`;
  }
  if (field === 'title') {
    const to = scalar(e.after);
    return to ? `renamed this to “${to}”` : 'edited the title';
  }
  if (field === 'parent_ticket_id') {
    const to = scalar(e.after);
    return to ? `moved this under ${to}` : 'removed the parent issue';
  }
  const count = listCount(e.after);
  if (count !== null) {
    if (count === 0) return `cleared the ${label}`;
    return `updated the ${label} (${count})`;
  }
  return label ? `edited the ${label}` : 'edited this ticket';
}

/** One readable sentence per event. */
export function describeTicketEvent(e: TicketEvent, names: UserNames = new Map()): EventSentence {
  const actor = actorName(e.actor);
  const key = issueKey(e);
  const jira = key ? `Jira ${key}` : 'Jira';
  switch (e.action) {
    case 'created':
      return { actor, text: 'created this ticket' };
    case 'status_changed': {
      const from = scalar(e.before);
      const to = scalar(e.after);
      if (from && to) return { actor, text: `moved this from ${formatStatusLabel(from)} to ${formatStatusLabel(to)}` };
      if (to) return { actor, text: `moved this to ${formatStatusLabel(to)}` };
      return { actor, text: 'changed the status' };
    }
    case 'assigned': {
      const to = assigneeText(e.after, names);
      return { actor, text: to ? `assigned this to ${to}` : 'assigned this ticket' };
    }
    case 'unassigned': {
      const from = assigneeText(e.before, names);
      return { actor, text: from ? `unassigned ${from}` : 'removed the assignee' };
    }
    case 'due_date_set': {
      const from = scalar(e.before);
      const to = scalar(e.after);
      if (!to) return { actor, text: 'cleared the due date' };
      if (from) return { actor, text: `moved the due date from ${formatDueDate(from)} to ${formatDueDate(to)}` };
      return { actor, text: `set the due date to ${formatDueDate(to)}` };
    }
    case 'estimate_set': {
      const from = scalar(e.before);
      const to = scalar(e.after);
      if (!to) return { actor, text: 'cleared the estimate' };
      if (from) return { actor, text: `changed the estimate from ${from} to ${to}` };
      return { actor, text: `set the estimate to ${to}` };
    }
    case 'field_edited':
      return { actor, text: fieldEdited(e) };
    case 'jira_linked':
      return { actor, text: `linked this to ${jira}` };
    case 'jira_published':
      return { actor, text: `published this to ${jira}` };
    case 'jira_unlinked':
      return { actor, text: `unlinked ${jira}` };
    case 'jira_pulled':
      return { actor, text: `took the title and description from ${jira}` };
    case 'approved':
      return { actor, text: 'approved this ticket' };
    case 'dismissed':
      return { actor, text: 'dismissed this ticket' };
    default:
      return { actor, text: 'updated this ticket' };
  }
}

/** Events that also leave a "system" diff comment from the same edit. */
export const EDIT_ACTIONS = new Set([
  'field_edited',
  'assigned',
  'unassigned',
  'due_date_set',
  'estimate_set',
  'status_changed',
]);
