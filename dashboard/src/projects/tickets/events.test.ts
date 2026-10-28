import { describe, expect, it } from 'vitest';
import type { TicketEvent } from '../../api/client';
import { actorName, describeTicketEvent } from './events';

const alice = { type: 'user', user_id: 'u_alice', display_name: 'Alice', persona: null, label: null };

function ev(action: string, extra: Partial<TicketEvent> = {}): TicketEvent {
  return { id: `evt_${action}`, action, field: null, before: null, after: null, actor: alice, created_at: '2026-10-20T10:00:00Z', ...extra };
}

const sentence = (e: TicketEvent, names?: Map<string, string>) => {
  const s = describeTicketEvent(e, names);
  return `${s.actor} ${s.text}`;
};

describe('actorName', () => {
  it('names each actor type', () => {
    expect(actorName(alice)).toBe('Alice');
    expect(actorName({ ...alice, persona: 'atlas' })).toBe('Alice (atlas)');
    expect(actorName({ type: 'user', user_id: 'u_x', display_name: null, persona: 'prism' })).toBe('prism');
    expect(actorName({ type: 'user', user_id: 'u_gone', display_name: null })).toBe('A teammate');
    expect(actorName({ type: 'service', label: 'ci-bot' })).toBe('Service key ci-bot');
    expect(actorName({ type: 'service' })).toBe('A service key');
    expect(actorName({ type: 'jira', label: 'Priya Shah' })).toBe('Jira (Priya Shah)');
    expect(actorName({ type: 'jira' })).toBe('Jira');
    expect(actorName({ type: 'system' })).toBe('SessionFS');
  });
});

describe('describeTicketEvent', () => {
  it('reads status moves', () => {
    expect(sentence(ev('status_changed', { field: 'status', before: 'open', after: 'in_progress' }))).toBe(
      'Alice moved this from open to in progress',
    );
  });

  it('reads persona and person assignments', () => {
    expect(sentence(ev('assigned', { field: 'assigned_to', after: { persona: 'reviewer' } }))).toBe(
      'Alice assigned this to the reviewer persona',
    );
    const names = new Map([['u_bob', 'Bob']]);
    expect(sentence(ev('assigned', { field: 'assigned_user_id', after: { user_id: 'u_bob' } }), names)).toBe(
      'Alice assigned this to Bob',
    );
    expect(sentence(ev('assigned', { field: 'assigned_user_id', after: { user_id: 'u_unknown' } }))).toBe(
      'Alice assigned this to a teammate',
    );
    expect(sentence(ev('unassigned', { field: 'assigned_to', before: { persona: 'atlas' } }))).toBe(
      'Alice unassigned the atlas persona',
    );
  });

  it('reads due dates and estimates, set, moved and cleared', () => {
    const year = new Date().getFullYear();
    expect(sentence(ev('due_date_set', { field: 'due_date', after: `${year}-11-15` }))).toBe('Alice set the due date to Nov 15');
    expect(sentence(ev('due_date_set', { field: 'due_date', before: `${year}-11-15`, after: `${year}-11-20` }))).toBe(
      'Alice moved the due date from Nov 15 to Nov 20',
    );
    expect(sentence(ev('due_date_set', { field: 'due_date', before: `${year}-11-15`, after: null }))).toBe('Alice cleared the due date');
    expect(sentence(ev('estimate_set', { field: 'estimated_effort', after: '2d 4h' }))).toBe('Alice set the estimate to 2d 4h');
    expect(sentence(ev('estimate_set', { field: 'estimated_effort', before: '1d', after: '2d' }))).toBe(
      'Alice changed the estimate from 1d to 2d',
    );
    expect(sentence(ev('estimate_set', { field: 'estimated_effort', before: '1d', after: null }))).toBe('Alice cleared the estimate');
  });

  it('reads field edits without ever showing a body', () => {
    expect(sentence(ev('field_edited', { field: 'description', before: { changed: true, length: 40 }, after: { changed: true, length: 52 } }))).toBe(
      'Alice edited the description',
    );
    expect(sentence(ev('field_edited', { field: 'acceptance_criteria', after: { changed: true, length: 3 } }))).toBe(
      'Alice edited the acceptance criteria',
    );
    expect(sentence(ev('field_edited', { field: 'priority', before: 'low', after: 'high' }))).toBe(
      'Alice changed the priority from low to high',
    );
    expect(sentence(ev('field_edited', { field: 'title', after: 'Ship it' }))).toBe('Alice renamed this to “Ship it”');
    expect(sentence(ev('field_edited', { field: 'title', after: { changed: true, length: 400 } }))).toBe('Alice edited the title');
    expect(sentence(ev('field_edited', { field: 'depends_on', after: { count: 3, items: ['tk_a', 'tk_b', 'tk_c'] } }))).toBe(
      'Alice updated the dependencies (3)',
    );
    expect(sentence(ev('field_edited', { field: 'file_refs', after: { count: 0, items: [] } }))).toBe('Alice cleared the file references');
  });

  it('reads Jira link events', () => {
    expect(sentence(ev('jira_linked', { after: { issue_key: 'ACME-12' } }))).toBe('Alice linked this to Jira ACME-12');
    expect(sentence(ev('jira_published', { after: { issue_key: 'ACME-12' } }))).toBe('Alice published this to Jira ACME-12');
    expect(sentence(ev('jira_unlinked', { before: { issue_key: 'ACME-12' } }))).toBe('Alice unlinked Jira ACME-12');
    expect(
      sentence(ev('jira_pulled', { actor: { type: 'jira', label: null }, after: { issue_key: 'ACME-12' } })),
    ).toBe('Jira took the title and description from Jira ACME-12');
  });

  it('reads moderation and creation', () => {
    expect(sentence(ev('approved'))).toBe('Alice approved this ticket');
    expect(sentence(ev('dismissed'))).toBe('Alice dismissed this ticket');
    expect(sentence(ev('created', { actor: { type: 'service', label: 'ci' } }))).toBe('Service key ci created this ticket');
  });

  it('falls back to plain words for unknown actions and shapes', () => {
    const odd = ev('teleported', { field: 'weird', before: { nested: { a: 1 } }, after: [1, 2] });
    const text = sentence(odd);
    expect(text).toBe('Alice updated this ticket');
    expect(text).not.toMatch(/[{}[\]]/);
    const oddField = sentence(ev('field_edited', { field: 'mystery_field', after: { nested: true } }));
    expect(oddField).toBe('Alice edited the mystery field');
    const oddStatus = sentence(ev('status_changed', { after: { x: 1 } }));
    expect(oddStatus).toBe('Alice changed the status');
  });
});
