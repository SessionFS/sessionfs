import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type ExternalContact } from '../../api/client';
import { axe } from '../../test/axe';
import ContactsSection from './ContactsSection';
import { ContactFormDialog } from './ContactFormDialog';
import { AssigneeLabel } from '../../projects/tickets/AssigneeLabel';
import { assigneePayload, decodeAssignee, encodeAssignee, ticketAssignee } from '../../projects/tickets/assignee';
import { participantName } from '../../meetings/people';
import { isResolved } from '../../meetings/format';
import { ParticipantPicker } from '../../meetings/Participants';

const ORG = 'org_1';

function contact(extra: Partial<ExternalContact> = {}): ExternalContact {
  return {
    id: 'ec_tolu',
    org_id: ORG,
    display_name: 'Tolulope Adeyemi',
    role: 'Clinical lead',
    tags: ['vendor'],
    aliases: ['Tolu'],
    status: 'active',
    email: 't***@clinic.example',
    email_masked: true,
    has_email: true,
    linked_user_id: null,
    linked_at: null,
    created_by_user_id: 'u_other',
    created_at: '2026-11-01T10:00:00Z',
    updated_at: '2026-11-07T10:00:00Z',
    redirected_from: null,
    possible_duplicates: [],
    ...extra,
  };
}

const TOLU = contact();
const DANA = contact({ id: 'ec_dana', display_name: 'Dana Smith', role: null, tags: [], aliases: [], email: null, email_masked: false, has_email: false });
const REFS = {
  contact_id: TOLU.id,
  conversations: [
    { kind: 'meeting' as const, meeting_id: 'mtg_1', participant_id: 'mp_1', title: 'Weekly sync', project_id: 'proj_1', group_id: null, meeting_at: '2026-11-05T15:00:00Z', created_at: '2026-11-05T16:00:00Z' },
  ],
  action_items: [
    { item_id: 'mai_1', meeting_id: 'mtg_1', kind: 'action' as const, status: 'pending' as const, description: 'Send the protocol draft', due_date: null },
  ],
  tickets: [{ id: 'tk_1', project_id: 'proj_1', title: 'Collect sign-off', status: 'open', updated_at: '2026-11-06T00:00:00Z' }],
};
const SUGGESTIONS = {
  truncated: false,
  groups: [
    {
      name: 'Priya',
      name_norm: 'priya',
      mention_count: 3,
      latest_mention_at: '2026-11-06T12:00:00Z',
      sources: [{ meeting_id: 'mtg_1', kind: 'meeting' as const, title: 'Weekly sync' }],
      candidates: [{ id: 'ec_dana', display_name: 'Dana Smith', hint: 'same first name' }],
    },
  ],
};

const client = {
  listContacts: vi.fn(),
  getContact: vi.fn(),
  getContactReferences: vi.fn(),
  createContact: vi.fn(),
  updateContact: vi.fn(),
  addContactAlias: vi.fn(),
  removeContactAlias: vi.fn(),
  mergeContacts: vi.fn(),
  deleteContact: vi.fn(),
  exportContact: vi.fn(),
  exportContactsCsv: vi.fn(),
  getContactSuggestions: vi.fn(),
  applyContactSuggestion: vi.fn(),
  dismissContactSuggestion: vi.fn(),
  resolveMeetingParticipant: vi.fn(),
};
const addToast = vi.fn();

vi.mock('../../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

beforeEach(() => {
  vi.clearAllMocks();
  client.listContacts.mockImplementation((_org: string, params: { q?: string }) =>
    Promise.resolve({ items: params?.q ? [] : [TOLU, DANA], next_before: null }),
  );
  client.getContact.mockImplementation((_org: string, id: string) => Promise.resolve(id === DANA.id ? DANA : TOLU));
  client.getContactReferences.mockResolvedValue(REFS);
  client.getContactSuggestions.mockResolvedValue(SUGGESTIONS);
  client.createContact.mockImplementation((_org: string, body: { display_name: string }) =>
    Promise.resolve(contact({ id: 'ec_new', display_name: body.display_name, aliases: [] })),
  );
  client.applyContactSuggestion.mockResolvedValue({ applied: 2, skipped: 1 });
  client.addContactAlias.mockResolvedValue(DANA);
  client.dismissContactSuggestion.mockResolvedValue(undefined);
  client.updateContact.mockResolvedValue({ ...TOLU, status: 'archived' });
});

function renderSection(path = '/settings/organization?tab=contacts', isAdmin = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ContactsSection orgId={ORG} isAdmin={isAdmin} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Contacts directory', () => {
  it('lists contacts with masked email and no admin actions for members', async () => {
    const { container } = renderSection();
    expect(await screen.findByRole('button', { name: /Tolulope Adeyemi/ })).toBeInTheDocument();
    expect(screen.getByText('t***@clinic.example')).toBeInTheDocument();
    expect(screen.getByText('(partly hidden)', { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Export CSV' })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('passes search, status and tag filters to the API', async () => {
    const user = userEvent.setup();
    renderSection();
    await screen.findByRole('button', { name: /Tolulope Adeyemi/ });
    await user.type(screen.getByRole('searchbox', { name: 'Search' }), 'zed');
    await waitFor(() =>
      expect(client.listContacts).toHaveBeenCalledWith(ORG, expect.objectContaining({ q: 'zed', status: 'active' })),
    );
    expect(await screen.findByText('No contacts match these filters.')).toBeInTheDocument();
  });

  it('shows Export CSV to admins', async () => {
    renderSection('/settings/organization?tab=contacts', true);
    expect(await screen.findByRole('button', { name: 'Export CSV' })).toBeInTheDocument();
  });
});

describe('Create contact', () => {
  function renderDialog(props: Partial<Parameters<typeof ContactFormDialog>[0]> = {}) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const onClose = vi.fn();
    const onSaved = vi.fn();
    render(
      <QueryClientProvider client={qc}>
        <ContactFormDialog orgId={ORG} onClose={onClose} onSaved={onSaved} {...props} />
      </QueryClientProvider>,
    );
    return { onClose, onSaved };
  }

  it('warns about a contact with the same name before creating', async () => {
    client.listContacts.mockResolvedValue({ items: [TOLU], next_before: null });
    const onUseExisting = vi.fn();
    renderDialog({ initialName: 'tolu', onUseExisting });
    expect(await screen.findByText(/already exists/)).toHaveTextContent('Tolulope Adeyemi');
    await userEvent.click(screen.getByRole('button', { name: 'Use Tolulope Adeyemi' }));
    expect(onUseExisting).toHaveBeenCalledWith('ec_tolu');
  });

  it('creates with tags and other names, then notes duplicates the server found', async () => {
    client.createContact.mockResolvedValue(contact({ id: 'ec_new', display_name: 'Sam Lee', possible_duplicates: [{ id: 'ec_x', display_name: 'Sam Lee' }] }));
    const user = userEvent.setup();
    const { onSaved } = renderDialog();
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Sam Lee');
    await user.type(screen.getByRole('textbox', { name: /Tags/ }), 'Vendor, data-team');
    await user.type(screen.getByRole('textbox', { name: /Other names/ }), 'Sammy, S. Lee');
    await user.click(screen.getByRole('button', { name: 'Create contact' }));
    expect(client.createContact).toHaveBeenCalledWith(ORG, {
      display_name: 'Sam Lee',
      email: null,
      role: null,
      tags: ['vendor', 'data-team'],
      aliases: ['Sammy', 'S. Lee'],
    });
    expect(await screen.findByText(/same name/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ id: 'ec_new' }));
  });

  it('explains an email that belongs to a member', async () => {
    client.createContact.mockRejectedValue(
      new ApiError(409, JSON.stringify({ error: { code: '409', message: 'x', details: { error: 'email_is_member' } } })),
    );
    const user = userEvent.setup();
    renderDialog({ initialName: 'Kim' });
    await user.click(screen.getByRole('button', { name: 'Create contact' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Assign the member instead');
  });
});

describe('Contact page', () => {
  it('shows other names and readable references, and archives after confirming', async () => {
    const user = userEvent.setup();
    const { container } = renderSection('/settings/organization?tab=contacts&contact=ec_tolu');
    expect(await screen.findByRole('heading', { name: /Tolulope Adeyemi/ })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Other names' })).toHaveTextContent('Tolu');
    expect(await screen.findByRole('link', { name: 'Collect sign-off' })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=tickets&ticket=tk_1',
    );
    expect(screen.getByRole('link', { name: 'Weekly sync' })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=meetings&meeting=mtg_1',
    );
    // Members get no merge/delete/export.
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    await user.click(screen.getByRole('button', { name: 'Archive' }));
    const dialog = await screen.findByRole('dialog', { name: /Archive Tolulope/ });
    await user.click(within(dialog).getByRole('button', { name: 'Archive' }));
    await waitFor(() => expect(client.updateContact).toHaveBeenCalledWith(ORG, 'ec_tolu', { status: 'archived' }));
  });

  it('adds and removes another name', async () => {
    client.addContactAlias.mockResolvedValue({ ...TOLU, aliases: ['Tolu', 'T. A.'] });
    client.removeContactAlias.mockResolvedValue({ ...TOLU, aliases: [] });
    const user = userEvent.setup();
    renderSection('/settings/organization?tab=contacts&contact=ec_tolu');
    await user.type(await screen.findByRole('textbox', { name: 'Add another name' }), 'T. A.');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(client.addContactAlias).toHaveBeenCalledWith(ORG, 'ec_tolu', 'T. A.'));
    await user.click(screen.getByRole('button', { name: 'Remove other name Tolu' }));
    await waitFor(() => expect(client.removeContactAlias).toHaveBeenCalledWith(ORG, 'ec_tolu', 'Tolu'));
  });

  it('admins delete with impact counts', async () => {
    client.deleteContact.mockResolvedValue({ deleted: true, contact_id: 'ec_tolu', participants_reset: 1, tickets_unassigned: 1 });
    const user = userEvent.setup();
    renderSection('/settings/organization?tab=contacts&contact=ec_tolu', true);
    await screen.findByRole('link', { name: 'Collect sign-off' });
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('dialog', { name: /Delete Tolulope/ });
    expect(dialog).toHaveTextContent('1 meeting or chat mention you can see go back to unmatched');
    expect(dialog).toHaveTextContent('1 ticket you can see becomes unassigned');
    await user.click(within(dialog).getByRole('button', { name: 'Delete contact' }));
    await waitFor(() => expect(client.deleteContact).toHaveBeenCalledWith(ORG, 'ec_tolu'));
  });

  it('admins merge a duplicate after a preview', async () => {
    client.mergeContacts.mockImplementation((_o: string, _t: string, _s: string, confirm: boolean) =>
      Promise.resolve({ confirmed: confirm, target: TOLU, source_id: 'ec_dana', participants_moved: 2, tickets_moved: 1, aliases_added: ['Dana Smith'] }),
    );
    const user = userEvent.setup();
    renderSection('/settings/organization?tab=contacts&contact=ec_tolu', true);
    await user.click(await screen.findByRole('button', { name: 'Merge…' }));
    const dialog = await screen.findByRole('dialog', { name: /Merge into/ });
    expect(within(dialog).getByRole('button', { name: 'Merge' })).toBeDisabled();
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate contact' }));
    await user.click(await screen.findByRole('option', { name: /Dana Smith/ }));
    expect(await within(dialog).findByTestId('merge-preview')).toHaveTextContent('2 meeting or chat mentions move');
    expect(client.mergeContacts).toHaveBeenCalledWith(ORG, 'ec_tolu', 'ec_dana', false);
    await user.click(within(dialog).getByRole('button', { name: 'Merge' }));
    await waitFor(() => expect(client.mergeContacts).toHaveBeenCalledWith(ORG, 'ec_tolu', 'ec_dana', true));
  });

  it('says so when the contact is gone', async () => {
    client.getContact.mockRejectedValue(new ApiError(404, JSON.stringify({ error: { code: '404', message: 'Contact not found' } })));
    renderSection('/settings/organization?tab=contacts&contact=ec_gone');
    expect(await screen.findByText(/doesn’t exist or was deleted/)).toBeInTheDocument();
  });
});

describe('To confirm', () => {
  it('matches a name to an existing contact and remembers it', async () => {
    const user = userEvent.setup();
    const { container } = renderSection('/settings/organization?tab=contacts&view=confirm');
    expect(await screen.findByText('“Priya”')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /To confirm/ })).toHaveTextContent('1');
    expect(screen.getByRole('link', { name: 'Weekly sync' })).toHaveAttribute('href', '/meetings/mtg_1');
    expect(await axe(container)).toHaveNoViolations();
    await user.click(screen.getByRole('button', { name: 'Existing contact…' }));
    await user.click(screen.getByRole('button', { name: /Who is “Priya”/ }));
    await user.click(await screen.findByRole('option', { name: /Dana Smith/ }));
    await user.click(screen.getByRole('button', { name: 'Match' }));
    await waitFor(() => expect(client.applyContactSuggestion).toHaveBeenCalledWith(ORG, 'Priya', 'ec_dana'));
    expect(client.addContactAlias).toHaveBeenCalledWith(ORG, 'ec_dana', 'Priya');
    expect(addToast).toHaveBeenCalledWith('success', expect.stringContaining('1 in conversations you can’t edit'));
  });

  it('creates a contact from a name and applies it', async () => {
    const user = userEvent.setup();
    renderSection('/settings/organization?tab=contacts&view=confirm');
    await user.click(await screen.findByRole('button', { name: 'Create contact' }));
    const dialog = await screen.findByRole('dialog', { name: 'New contact' });
    expect(within(dialog).getByRole('textbox', { name: 'Name' })).toHaveValue('Priya');
    await user.click(within(dialog).getByRole('button', { name: 'Create contact' }));
    await waitFor(() => expect(client.applyContactSuggestion).toHaveBeenCalledWith(ORG, 'Priya', 'ec_new'));
  });

  it('dismisses a name', async () => {
    const user = userEvent.setup();
    renderSection('/settings/organization?tab=contacts&view=confirm');
    await user.click(await screen.findByRole('button', { name: 'Dismiss “Priya”' }));
    await waitFor(() => expect(client.dismissContactSuggestion).toHaveBeenCalledWith(ORG, 'Priya'));
  });
});

describe('Contacts on tickets and meetings', () => {
  const ref = { id: 'ec_tolu', display_name: 'Tolulope Adeyemi', role: 'Clinical lead', status: 'active' as const, linked_user_id: null, external: true as const };

  it('encodes a contact assignee and sends it alone', () => {
    const a = decodeAssignee(encodeAssignee({ kind: 'contact', contactId: 'ec_tolu' }));
    expect(a).toEqual({ kind: 'contact', contactId: 'ec_tolu' });
    expect(assigneePayload(a)).toEqual({ assigned_to: null, assigned_user_id: null, assigned_contact_id: 'ec_tolu' });
    // Moving off a contact clears it explicitly; otherwise the old shape.
    expect(assigneePayload({ kind: 'person', userId: 'u1' }, { clearContact: true })).toEqual({
      assigned_user_id: 'u1',
      assigned_to: null,
      assigned_contact_id: null,
    });
    expect(assigneePayload({ kind: 'none' })).toEqual({ assigned_to: null, assigned_user_id: null });
    expect(ticketAssignee({ assigned_to: null, assigned_user_id: null, assigned_contact_id: 'ec_tolu' })).toEqual(a);
  });

  it('labels a contact assignee as external, and a removed one as unassigned', () => {
    const { rerender } = render(
      <AssigneeLabel ticket={{ assigned_to: null, assigned_user: null, assigned_user_id: null, assigned_contact_id: 'ec_tolu', assigned_contact: ref }} />,
    );
    expect(screen.getByText('Tolulope Adeyemi')).toBeInTheDocument();
    expect(screen.getByTestId('external-badge')).toHaveTextContent('external');
    rerender(<AssigneeLabel ticket={{ assigned_to: null, assigned_user: null, assigned_user_id: null, assigned_contact_id: 'ec_tolu', assigned_contact: null }} />);
    expect(screen.getByText('Unassigned (contact removed)')).toBeInTheDocument();
  });

  it('shows a participant matched to a contact by the contact name', () => {
    const p = { id: 'mp_1', name_raw: 'Tolu', email_raw: null, user_id: null, persona_name: null, resolution_method: 'contact' as const, resolved_by_user_id: null, contact_id: 'ec_tolu', contact: ref };
    expect(isResolved(p)).toBe(true);
    expect(participantName(p, { nameOf: () => null })).toBe('Tolulope Adeyemi');
    expect(isResolved({ ...p, contact: null })).toBe(false);
  });

  it('matches a meeting participant to a contact, or creates one from the name', async () => {
    client.resolveMeetingParticipant.mockResolvedValue({});
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const p = { id: 'mp_2', name_raw: 'Priya', email_raw: null, user_id: null, persona_name: null, resolution_method: 'unresolved' as const, resolved_by_user_id: null };
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={qc}>
        <ParticipantPicker
          meetingId="mtg_1"
          participant={p}
          people={{ people: [], personas: [], contacts: [DANA], contactsOrgId: ORG, loading: false, nameOf: () => null }}
          onDone={onDone}
        />
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole('button', { name: /Match “Priya” to/ }));
    expect(screen.getByRole('group', { name: 'Contacts' })).toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: /Dana Smith/ }));
    await waitFor(() => expect(client.resolveMeetingParticipant).toHaveBeenCalledWith('mtg_1', 'mp_2', { contact_id: 'ec_dana' }));

    await user.click(screen.getByRole('button', { name: 'New contact' }));
    const dialog = await screen.findByRole('dialog', { name: 'New contact' });
    expect(within(dialog).getByRole('textbox', { name: 'Name' })).toHaveValue('Priya');
    await user.click(within(dialog).getByRole('button', { name: 'Create contact' }));
    await waitFor(() => expect(client.resolveMeetingParticipant).toHaveBeenCalledWith('mtg_1', 'mp_2', { contact_id: 'ec_new' }));
  });
});
