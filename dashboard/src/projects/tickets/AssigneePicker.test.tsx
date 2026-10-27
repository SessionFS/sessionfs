import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type Persona } from '../../api/client';
import { axe } from '../../test/axe';
import { AssigneePicker } from './AssigneePicker';
import {
  UNASSIGNED,
  assigneeErrorFrom,
  assigneeNote,
  assigneePayload,
  decodeAssignee,
  encodeAssignee,
  ticketAssignee,
  type Assignee,
} from './assignee';

const { mockUseAssignableUsers, mockUsePersonas, mockUseMe } = vi.hoisted(() => ({
  mockUseAssignableUsers: vi.fn(),
  mockUsePersonas: vi.fn(),
  mockUseMe: vi.fn(),
}));

vi.mock('../../hooks/useTickets', () => ({ useAssignableUsers: mockUseAssignableUsers }));
vi.mock('../../hooks/usePersonas', () => ({ usePersonas: mockUsePersonas }));
vi.mock('../../hooks/useMe', () => ({ useMe: mockUseMe }));

const USERS = [
  { id: 'u2', display_name: 'Sam Rivera', email: 'sam@example.test', can_edit: true },
  { id: 'u1', display_name: 'Ola Dev', email: 'ola@example.test', can_edit: true },
  { id: 'u3', display_name: null, email: 'kim@example.test', can_edit: false },
];

function persona(overrides: Partial<Persona> = {}): Persona {
  return {
    id: 'pers_1', project_id: 'proj_1', org_id: null, scope: 'project', name: 'atlas', role: 'Backend engineer',
    content: '', specializations: [], is_active: true, version: 1, created_by: 'u1', represented_user_id: null,
    created_at: '2026-05-14T00:00:00Z', updated_at: '2026-05-14T00:00:00Z',
    ...overrides,
  };
}

const PERSONAS: Persona[] = [
  persona(),
  persona({ id: 'pers_2', project_id: null, org_id: 'org_1', scope: 'org', name: 'sentinel', role: 'Security reviewer', represented_user_id: 'u2' }),
  persona({ id: 'pers_3', project_id: null, org_id: 'org_1', scope: 'org', name: 'compass', role: 'Product', represented_user_id: null }),
  persona({ id: 'pers_4', name: 'retired', is_active: false }),
];

beforeEach(() => {
  mockUseAssignableUsers.mockReturnValue({ data: USERS, isLoading: false });
  mockUsePersonas.mockReturnValue({ data: PERSONAS, isLoading: false });
  mockUseMe.mockReturnValue({ data: { user_id: 'u1' } });
});

function Harness({ initial = UNASSIGNED, onChange }: { initial?: Assignee; onChange?: (a: Assignee) => void }) {
  const [value, setValue] = useState<Assignee>(initial);
  return (
    <MemoryRouter>
      <AssigneePicker
        projectId="proj_1"
        value={value}
        onChange={(a) => {
          setValue(a);
          onChange?.(a);
        }}
      />
    </MemoryRouter>
  );
}

async function openPicker() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Assignee' }));
  return user;
}

describe('assignee helpers', () => {
  it('round-trips option values', () => {
    for (const a of [UNASSIGNED, { kind: 'person', userId: 'u1' }, { kind: 'persona', name: 'atlas' }] as Assignee[]) {
      expect(decodeAssignee(encodeAssignee(a))).toEqual(a);
    }
    expect(decodeAssignee('user:')).toEqual(UNASSIGNED);
  });

  it('reads the assignee off a ticket, person first', () => {
    expect(ticketAssignee({ assigned_to: 'atlas', assigned_user_id: null })).toEqual({ kind: 'persona', name: 'atlas' });
    expect(ticketAssignee({ assigned_to: null, assigned_user_id: 'u2' })).toEqual({ kind: 'person', userId: 'u2' });
    expect(ticketAssignee({ assigned_to: null })).toEqual(UNASSIGNED);
  });

  it('never sends both fields non-null', () => {
    expect(assigneePayload({ kind: 'person', userId: 'u2' })).toEqual({ assigned_user_id: 'u2', assigned_to: null });
    expect(assigneePayload({ kind: 'persona', name: 'atlas' })).toEqual({ assigned_to: 'atlas', assigned_user_id: null });
    expect(assigneePayload(UNASSIGNED)).toEqual({ assigned_to: null, assigned_user_id: null });
  });

  it('warns for a persona that represents no one', () => {
    const ctx = { personas: PERSONAS, users: USERS, meId: 'u1' };
    expect(assigneeNote({ kind: 'persona', name: 'atlas' }, ctx)).toEqual({
      kind: 'unrepresented', persona: 'atlas', orgId: null, orgPersona: false,
    });
    expect(assigneeNote({ kind: 'persona', name: 'compass' }, ctx)).toEqual({
      kind: 'unrepresented', persona: 'compass', orgId: 'org_1', orgPersona: true,
    });
  });

  it('names who a represented persona or a person notifies', () => {
    const ctx = { personas: PERSONAS, users: USERS, meId: 'u1' };
    expect(assigneeNote({ kind: 'persona', name: 'sentinel' }, ctx)).toEqual({ kind: 'notifies', name: 'Sam Rivera', you: false });
    expect(assigneeNote({ kind: 'person', userId: 'u3' }, ctx)).toEqual({ kind: 'notifies', name: 'kim@example.test', you: false });
    expect(assigneeNote({ kind: 'person', userId: 'u1' }, ctx)).toEqual({ kind: 'notifies', name: 'Ola Dev', you: true });
  });

  it('says nothing when unassigned or the persona is unknown', () => {
    const ctx = { personas: PERSONAS, users: USERS, meId: 'u1' };
    expect(assigneeNote(UNASSIGNED, ctx)).toBeNull();
    expect(assigneeNote({ kind: 'persona', name: 'ghost' }, ctx)).toBeNull();
    expect(assigneeNote({ kind: 'persona', name: 'atlas' }, { ...ctx, personas: undefined })).toBeNull();
  });

  it('maps assignee error codes from either envelope field', () => {
    const coded = new ApiError(422, JSON.stringify({ error: { code: 'assignee_not_eligible', message: 'nope' } }));
    expect(assigneeErrorFrom(coded)).toMatch(/can't be assigned tickets in this project/);
    const detailed = new ApiError(422, JSON.stringify({ error: { code: '422', message: 'x', details: { error: 'assignee_conflict' } } }));
    expect(assigneeErrorFrom(detailed)).toMatch(/person or a persona, not both/);
    expect(assigneeErrorFrom(new ApiError(500, 'boom'))).toBeNull();
    expect(assigneeErrorFrom(new Error('x'))).toBeNull();
  });
});

describe('AssigneePicker', () => {
  it('groups people (you first) and active personas under Unassigned', async () => {
    render(<Harness />);
    await openPicker();
    const listbox = screen.getByRole('listbox', { name: 'Assignee' });
    const options = within(listbox).getAllByRole('option');
    expect(options[0]).toHaveTextContent('Unassigned');

    const people = within(listbox).getByRole('group', { name: 'People' });
    const peopleNames = within(people).getAllByRole('option').map((o) => o.textContent);
    expect(peopleNames[0]).toMatch(/Ola Dev.*ola@example\.test.*You/);
    expect(peopleNames[1]).toMatch(/kim@example\.test/);
    expect(peopleNames[2]).toMatch(/Sam Rivera.*sam@example\.test/);

    const roles = within(listbox).getByRole('group', { name: 'Personas' });
    const roleNames = within(roles).getAllByRole('option').map((o) => o.textContent);
    expect(roleNames).toEqual([
      expect.stringMatching(/^atlas.*Backend engineer/),
      expect.stringMatching(/^compass/),
      expect.stringMatching(/^sentinel/),
    ]);
    expect(within(listbox).queryByText('retired')).toBeNull();
  });

  it('filters as you type by name, email or role — not by option ids', async () => {
    render(<Harness />);
    const user = await openPicker();
    const search = screen.getByRole('combobox', { name: 'Search assignee' });
    await user.type(search, 'sam@');
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([expect.stringMatching(/Sam Rivera/)]);
    await user.clear(search);
    await user.type(search, 'security');
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([expect.stringMatching(/sentinel/)]);
    await user.clear(search);
    // "user"/"persona" prefixes of option values must not match everything.
    await user.type(search, 'persona');
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    expect(screen.getByRole('status')).toHaveTextContent(/No matches/);
  });

  it('picks with the keyboard via aria-activedescendant', async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = await openPicker();
    const search = screen.getByRole('combobox', { name: 'Search assignee' });
    expect(search).toHaveFocus();
    await user.type(search, 'sam');
    const active = search.getAttribute('aria-activedescendant');
    expect(active && document.getElementById(active)).toHaveTextContent(/Sam Rivera/);
    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenLastCalledWith({ kind: 'person', userId: 'u2' });
    const trigger = screen.getByRole('button', { name: 'Assignee' });
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveTextContent('Sam Rivera');
  });

  it('unassigns', async () => {
    const onChange = vi.fn();
    render(<Harness initial={{ kind: 'persona', name: 'atlas' }} onChange={onChange} />);
    const user = await openPicker();
    await user.click(screen.getByRole('option', { name: /Unassigned/ }));
    expect(onChange).toHaveBeenLastCalledWith(UNASSIGNED);
    expect(screen.queryByTestId('assignee-note')).toBeNull();
  });

  it('warns under the field when the persona represents no one, linking org personas to settings', async () => {
    render(<Harness initial={{ kind: 'persona', name: 'compass' }} />);
    const note = screen.getByTestId('assignee-note');
    expect(note).toHaveTextContent(
      'No one is notified: compass doesn’t represent a person. Assign a person, or set who this persona represents in persona settings.',
    );
    expect(within(note).getByRole('link', { name: 'persona settings' })).toHaveAttribute(
      'href',
      '/settings/organization?tab=personas&org=org_1',
    );
    // The note describes the field for screen readers.
    const trigger = screen.getByRole('button', { name: 'Assignee' });
    expect(trigger).toHaveAccessibleDescription(/No one is notified/);
  });

  it('suggests an org persona for a project persona that cannot represent anyone', () => {
    render(<Harness initial={{ kind: 'persona', name: 'atlas' }} />);
    const note = screen.getByTestId('assignee-note');
    expect(note).toHaveTextContent(/No one is notified: atlas doesn’t represent a person/);
    expect(within(note).queryByRole('link')).toBeNull();
  });

  it('shows who is notified for a person or a represented persona', async () => {
    render(<Harness initial={{ kind: 'persona', name: 'sentinel' }} />);
    expect(screen.getByTestId('assignee-note')).toHaveTextContent('Notifies Sam Rivera');
    const user = await openPicker();
    await user.click(screen.getByRole('option', { name: /Ola Dev/ }));
    expect(screen.getByTestId('assignee-note')).toHaveTextContent('Notifies you');
    expect(screen.getByTestId('assignee-note')).not.toHaveAttribute('data-tone');
  });

  it('offers personas only when the caller may not list people (403)', async () => {
    mockUseAssignableUsers.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new ApiError(403, JSON.stringify({ error: { code: '403', message: 'no', details: { error: 'project_member_required' } } })),
    });
    render(<Harness />);
    await openPicker();
    const listbox = screen.getByRole('listbox', { name: 'Assignee' });
    expect(within(listbox).queryByRole('group', { name: 'People' })).toBeNull();
    expect(within(listbox).getByRole('group', { name: 'Personas' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/People aren’t listed/);
  });

  it('says when people could not be loaded for another reason', async () => {
    mockUseAssignableUsers.mockReturnValue({ data: undefined, isLoading: false, error: new ApiError(500, 'boom') });
    render(<Harness />);
    await openPicker();
    expect(screen.getByRole('status')).toHaveTextContent(/People couldn’t be loaded/);
  });

  it('has no axe violations while open', async () => {
    const { container } = render(<Harness initial={{ kind: 'persona', name: 'compass' }} />);
    await openPicker();
    expect(await axe(container)).toHaveNoViolations();
  });
});
