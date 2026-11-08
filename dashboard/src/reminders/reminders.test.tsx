import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type Reminder } from '../api/client';
import { axe } from '../test/axe';
import TicketInbox from '../notifications/TicketInbox';
import RemindersPage from './RemindersPage';
import { RemindersStrip } from './RemindersStrip';
import { fromLocalInput, sortReminders } from './format';

const { client, toast } = vi.hoisted(() => ({
  client: {
    listReminders: vi.fn(),
    createReminder: vi.fn(),
    getReminder: vi.fn(),
    patchReminder: vi.fn(),
    deleteReminder: vi.fn(),
    snoozeReminder: vi.fn(),
    listReminderNotes: vi.fn(),
    addReminderNote: vi.fn(),
    listMeetingReminders: vi.fn(),
    listGroupMeetingReminders: vi.fn(),
    listTicketReminders: vi.fn(),
    listTicketNotifications: vi.fn(),
    markTicketNotificationsRead: vi.fn(),
  },
  toast: { addToast: vi.fn() },
}));

vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ auth: { client } }) }));
vi.mock('../hooks/useToast', () => ({ useToast: () => toast }));

function rem(over: Partial<Reminder> = {}): Reminder {
  return {
    id: 'rem_1',
    title: 'Book follow-up with Tolu',
    status: 'open',
    due_at: '2030-11-06T09:00:00Z',
    original_due_at: '2030-11-06T09:00:00Z',
    timezone: 'Europe/London',
    is_due: false,
    owner_user_id: 'u1',
    created_by_user_id: 'u1',
    created_via: 'manual',
    is_owner: true,
    is_creator: true,
    source: null,
    source_hidden: false,
    linked_ticket_id: null,
    notified_at: null,
    snooze_count: 0,
    closed_at: null,
    closed_by_user_id: null,
    note_count: 0,
    created_at: '2026-10-31T10:00:00Z',
    updated_at: '2026-10-31T10:00:00Z',
    ...over,
  };
}

function renderAt(ui: React.ReactElement, url = '/reminders') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const due = rem({ id: 'rem_due', title: 'Overdue thing', is_due: true, due_at: '2026-10-01T09:00:00Z' });
const later = rem({ id: 'rem_later', title: 'Later thing' });

beforeEach(() => {
  for (const f of Object.values(client)) f.mockReset();
  toast.addToast.mockReset();
  client.listReminders.mockImplementation(async ({ status }: { status: string }) => ({
    items: status === 'open' ? [later, due] : [],
    next_cursor: null,
  }));
  client.getReminder.mockResolvedValue(rem());
  client.listReminderNotes.mockResolvedValue({ items: [] });
  client.patchReminder.mockResolvedValue(rem({ status: 'done' }));
  client.snoozeReminder.mockResolvedValue(rem());
  client.deleteReminder.mockResolvedValue(undefined);
  client.createReminder.mockResolvedValue(rem());
  client.addReminderNote.mockResolvedValue({ id: 'rn_1', reminder_id: 'rem_1', author_user_id: 'u1', body: 'x', created_at: '2026-10-31T10:00:00Z' });
});

describe('RemindersPage', () => {
  it('lists open reminders with due ones first and marked', async () => {
    renderAt(<RemindersPage />);
    const list = await screen.findByRole('list', { name: 'open reminders' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('Overdue thing');
    expect(within(rows[0]).getByText('Due')).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent('Later thing');
    expect(within(rows[1]).queryByText('Due')).not.toBeInTheDocument();
    expect(rows[1]).toHaveTextContent('Europe/London');
  });

  it('switches status tabs', async () => {
    const user = userEvent.setup();
    renderAt(<RemindersPage />);
    await screen.findByRole('list', { name: 'open reminders' });
    await user.click(screen.getByRole('tab', { name: 'Done' }));
    await waitFor(() => expect(client.listReminders).toHaveBeenCalledWith(expect.objectContaining({ status: 'done' })));
    expect(await screen.findByText('No done reminders.')).toBeInTheDocument();
  });

  it('opens the drawer from the deep link', async () => {
    client.getReminder.mockResolvedValue(rem({ id: 'rem_9', title: 'From the digest' }));
    renderAt(<RemindersPage />, '/reminders?reminder=rem_9');
    const dlg = await screen.findByRole('dialog', { name: 'From the digest' });
    expect(client.getReminder).toHaveBeenCalledWith('rem_9');
    expect(dlg).toHaveTextContent('6 Nov 2030, 09:00');
    expect(dlg).toHaveTextContent('Europe/London');
  });

  it('posts the add dialog body in the user timezone', async () => {
    const user = userEvent.setup();
    renderAt(<RemindersPage />);
    await user.click(screen.getByRole('button', { name: 'Add reminder' }));
    const dlg = await screen.findByRole('dialog', { name: 'Add reminder' });
    expect(dlg).toHaveTextContent(/Your time zone: /);
    await user.type(within(dlg).getByLabelText('Title'), 'Call the vendor');
    fireEvent.change(within(dlg).getByLabelText('Due'), { target: { value: '2030-11-06T09:00' } });
    await user.click(within(dlg).getByRole('button', { name: 'Add reminder' }));
    await waitFor(() => expect(client.createReminder).toHaveBeenCalled());
    const body = client.createReminder.mock.calls[0][0];
    expect(body.title).toBe('Call the vendor');
    expect(body.due_at).toBe(fromLocalInput('2030-11-06T09:00'));
    expect(typeof body.timezone).toBe('string');
  });

  it('has no axe violations with the drawer open', async () => {
    const { container } = renderAt(<RemindersPage />, '/reminders?reminder=rem_1');
    await screen.findByRole('dialog', { name: 'Book follow-up with Tolu' });
    expect(await axe(document.body)).toHaveNoViolations();
    expect(container).toBeTruthy();
  });
});

describe('ReminderDrawer actions', () => {
  async function openDrawer(r: Reminder) {
    client.getReminder.mockResolvedValue(r);
    renderAt(<RemindersPage />, `/reminders?reminder=${r.id}`);
    return screen.findByRole('dialog', { name: r.title });
  }

  it('marks done through PATCH', async () => {
    const user = userEvent.setup();
    const dlg = await openDrawer(rem());
    await user.click(within(dlg).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(client.patchReminder).toHaveBeenCalledWith('rem_1', { status: 'done' }));
  });

  it('confirms before dismissing', async () => {
    const user = userEvent.setup();
    const dlg = await openDrawer(rem());
    await user.click(within(dlg).getByRole('button', { name: 'Dismiss' }));
    expect(client.patchReminder).not.toHaveBeenCalled();
    const confirm = await screen.findByRole('dialog', { name: 'Dismiss this reminder?' });
    await user.click(within(confirm).getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(client.patchReminder).toHaveBeenCalledWith('rem_1', { status: 'dismissed' }));
  });

  it('snoozes by preset and with a custom time', async () => {
    const user = userEvent.setup();
    const dlg = await openDrawer(rem());
    await user.click(within(dlg).getByRole('button', { name: 'Snooze' }));
    await user.click(screen.getByRole('menuitem', { name: '1 hour' }));
    await waitFor(() => expect(client.snoozeReminder).toHaveBeenCalledWith('rem_1', { preset: '1h' }));

    await user.click(within(dlg).getByRole('button', { name: 'Snooze' }));
    await user.click(screen.getByRole('menuitem', { name: 'Custom…' }));
    const custom = await screen.findByRole('dialog', { name: 'Snooze until' });
    fireEvent.change(within(custom).getByLabelText('Date and time'), { target: { value: '2030-12-01T08:30' } });
    await user.click(within(custom).getByRole('button', { name: 'Snooze' }));
    await waitFor(() =>
      expect(client.snoozeReminder).toHaveBeenLastCalledWith('rem_1', {
        preset: 'custom',
        until: fromLocalInput('2030-12-01T08:30'),
      }),
    );
  });

  it('reschedules and deletes (creator)', async () => {
    const user = userEvent.setup();
    const dlg = await openDrawer(rem());
    await user.click(within(dlg).getByRole('button', { name: 'Reschedule' }));
    const re = await screen.findByRole('dialog', { name: 'Reschedule' });
    fireEvent.change(within(re).getByLabelText('New date and time'), { target: { value: '2030-12-02T10:00' } });
    await user.click(within(re).getByRole('button', { name: 'Reschedule' }));
    await waitFor(() =>
      expect(client.patchReminder).toHaveBeenCalledWith(
        'rem_1',
        expect.objectContaining({ due_at: fromLocalInput('2030-12-02T10:00') }),
      ),
    );
    await user.click(within(dlg).getByRole('button', { name: 'Delete' }));
    const del = await screen.findByRole('dialog', { name: 'Delete this reminder?' });
    await user.click(within(del).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(client.deleteReminder).toHaveBeenCalledWith('rem_1'));
  });

  it('hides owner-only controls from a creator who is not the owner', async () => {
    const dlg = await openDrawer(rem({ is_owner: false, is_creator: true }));
    expect(within(dlg).queryByRole('button', { name: 'Snooze' })).not.toBeInTheDocument();
    expect(within(dlg).queryByRole('button', { name: 'Reschedule' })).not.toBeInTheDocument();
    expect(within(dlg).getByRole('button', { name: 'Edit title' })).toBeInTheDocument();
    expect(within(dlg).getByRole('button', { name: 'Done' })).toBeInTheDocument();
    expect(within(dlg).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('hides creator-only controls from an owner who did not create it', async () => {
    const dlg = await openDrawer(rem({ is_owner: true, is_creator: false }));
    expect(within(dlg).getByRole('button', { name: 'Reschedule' })).toBeInTheDocument();
    expect(within(dlg).queryByRole('button', { name: 'Edit title' })).not.toBeInTheDocument();
    expect(within(dlg).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('offers no actions on a closed reminder', async () => {
    const dlg = await openDrawer(rem({ status: 'done' }));
    expect(within(dlg).queryByRole('button', { name: 'Done' })).not.toBeInTheDocument();
    expect(within(dlg).queryByRole('button', { name: 'Snooze' })).not.toBeInTheDocument();
  });

  it('shows a server error such as reminder_not_open', async () => {
    const user = userEvent.setup();
    client.patchReminder.mockRejectedValue(
      new ApiError(409, JSON.stringify({ error: { code: 'reminder_not_open', message: 'This reminder is already closed.' } })),
    );
    const dlg = await openDrawer(rem());
    await user.click(within(dlg).getByRole('button', { name: 'Done' }));
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('This reminder is already closed.');
  });

  it('adds a note', async () => {
    const user = userEvent.setup();
    const dlg = await openDrawer(rem());
    await user.type(within(dlg).getByLabelText('Add a note'), 'Ping on Slack');
    await user.click(within(dlg).getByRole('button', { name: 'Add note' }));
    await waitFor(() => expect(client.addReminderNote).toHaveBeenCalledWith('rem_1', 'Ping on Slack'));
  });

  it('links the source, or says it is gone', async () => {
    const src = { kind: 'meeting', id: 'mtg_1', lines: '4-6', title: 'Weekly sync', project_id: 'proj_1', group_id: null, meeting_id: 'mtg_1' };
    let dlg = await openDrawer(rem({ source: src }));
    expect(within(dlg).getByRole('link', { name: /Weekly sync/ })).toHaveAttribute('href', '/meetings/mtg_1?lines=4-6');
    document.body.innerHTML = '';
    dlg = await openDrawer(rem({ id: 'rem_2', title: 'Hidden one', source: null, source_hidden: true }));
    expect(dlg).toHaveTextContent('Source no longer available');
  });
});

describe('sortReminders', () => {
  it('puts due first then by time', () => {
    const out = sortReminders([later, due]);
    expect(out.map((r) => r.id)).toEqual(['rem_due', 'rem_later']);
  });
});

describe('inbox rows', () => {
  it('renders a reminder row next to a ticket row', async () => {
    const base = {
      actor_type: 'user', actor_user_id: null, service_key_name: null, event_count: 1,
      created_at: '2026-10-31T10:00:00Z', updated_at: '2026-10-31T10:00:00Z', read_at: null,
    };
    client.listTicketNotifications.mockResolvedValue({
      items: [
        { ...base, id: 2, subject_kind: 'reminder', subject: { kind: 'reminder', id: 'rem_7', title: 'Renew the cert', due_at: '2030-11-06T09:00:00Z' }, ticket_id: null, project_id: null, ticket_title: null, kind: 'reminder_due', summary: 'Reminder is due' },
        { ...base, id: 1, subject_kind: 'ticket', subject: { kind: 'ticket', id: 'tk_1', title: 'Add export' }, ticket_id: 'tk_1', project_id: 'proj_1', ticket_title: 'Add export', kind: 'your_turn', summary: 'Ready' },
      ],
      next_before: null,
    });
    renderAt(<TicketInbox />, '/inbox');
    const rem7 = await screen.findByRole('link', { name: /Renew the cert/ });
    expect(rem7).toHaveAttribute('href', '/reminders?reminder=rem_7');
    expect(screen.getByText(/^due /)).toBeInTheDocument();
    expect(screen.getByText('Reminder')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add export' })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=tickets&ticket=tk_1',
    );
  });
});

describe('RemindersStrip', () => {
  it('is hidden when there are no reminders', async () => {
    client.listTicketReminders.mockResolvedValue({ items: [], counts: { open: 0, done: 0, dismissed: 0 } });
    renderAt(<RemindersStrip kind="ticket" projectId="proj_1" sourceId="tk_1" />);
    await waitFor(() => expect(client.listTicketReminders).toHaveBeenCalledWith('proj_1', 'tk_1'));
    expect(screen.queryByRole('region', { name: 'Reminders' })).not.toBeInTheDocument();
  });

  it('shows counts and the list on a meeting', async () => {
    client.listMeetingReminders.mockResolvedValue({
      items: [rem({ id: 'rem_a', title: 'Chase legal' }), rem({ id: 'rem_b', title: 'Sent deck', status: 'done' })],
      counts: { open: 1, done: 1, dismissed: 0 },
    });
    renderAt(<RemindersStrip kind="meeting" projectId="proj_1" sourceId="mtg_1" />);
    const strip = await screen.findByRole('region', { name: 'Reminders' });
    expect(strip).toHaveTextContent('1 open, 1 done, 0 dismissed');
    expect(within(strip).getByRole('link', { name: 'Chase legal' })).toHaveAttribute('href', '/reminders?reminder=rem_a');
    expect(client.listMeetingReminders).toHaveBeenCalledWith('proj_1', 'mtg_1');
  });

  it('reads a group meeting through its group', async () => {
    client.listGroupMeetingReminders.mockResolvedValue({
      items: [rem({ id: 'rem_g', title: 'Send the group recap' })],
      counts: { open: 1, done: 0, dismissed: 0 },
    });
    renderAt(<RemindersStrip kind="meeting" projectId={undefined} groupId="grp_1" sourceId="mtg_9" />);
    const strip = await screen.findByRole('region', { name: 'Reminders' });
    expect(within(strip).getByRole('link', { name: 'Send the group recap' })).toBeInTheDocument();
    expect(client.listGroupMeetingReminders).toHaveBeenCalledWith('grp_1', 'mtg_9');
    expect(client.listMeetingReminders).not.toHaveBeenCalledWith(undefined, 'mtg_9');
  });
});
