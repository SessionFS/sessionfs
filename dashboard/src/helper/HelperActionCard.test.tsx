import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type HelperAction } from '../api/client';
import { axe } from '../test/axe';
import type { TurnAction } from './actions';

const { useAuthMock } = vi.hoisted(() => ({ useAuthMock: vi.fn() }));
vi.mock('../auth/AuthContext', () => ({ useAuth: useAuthMock }));

import HelperActionCard from './HelperActionCard';

const ID = 'ha_0123456789abcdef01234567';
const inMinutes = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

const TRASH: TurnAction = {
  id: ID,
  kind: 'trash_sessions',
  summary: 'Move 2 session(s) to Trash',
  expires_at: inMinutes(14),
  targets: [
    { id: 'ses_a', name: 'Fix webhook retry', change: 'will_change', before: 'active', after: 'trashed', tool: 'claude-code', model: 'deepseek-v4-pro', created_at: '2026-10-01T10:00:00Z' },
    { id: 'ses_b', name: 'Scratch notes', change: 'will_change', before: 'active', after: 'trashed' },
    { id: 'ses_c', name: 'Old spike', change: 'skipped', reason: 'already_trashed' },
  ],
};

const NARRATIVE: TurnAction = {
  id: ID,
  kind: 'set_auto_narrative',
  summary: 'Set auto-narrative on for 1 project(s)',
  expires_at: inMinutes(14),
  targets: [
    { id: 'proj_1', name: 'Acme', change: 'will_change', before: false, after: true },
    { id: 'proj_2', name: 'Docs', change: 'unchanged', before: true, after: true },
  ],
};

const CREATE_TICKET: TurnAction = {
  id: ID,
  kind: 'create_ticket',
  summary: 'Create a task ticket in Acme',
  expires_at: inMinutes(14),
  targets: [{ id: 'proj_1', name: 'Acme', change: 'will_change' }],
  params: {
    project_id: 'proj_1',
    kind: 'task',
    title: 'Fix webhook retry',
    description: 'Retry with backoff.',
    acceptance_criteria: ['Retries 3 times', 'Logs failures'],
    priority: 'high',
    assigned_to: 'Atlas',
  },
};

const ASSIGN_TICKET: TurnAction = {
  id: ID,
  kind: 'assign_ticket',
  summary: 'Assign Fix webhook retry to Atlas',
  expires_at: inMinutes(14),
  targets: [
    { id: 'tk_1', name: 'Fix webhook retry', change: 'will_change', before: null, after: 'Atlas' },
    { id: 'tk_2', name: 'Scratch notes', change: 'unchanged', before: 'Atlas', after: 'Atlas' },
  ],
  params: { ticket_id: 'tk_1', assigned_to: 'Atlas' },
};

const COMMENT_TICKET: TurnAction = {
  id: ID,
  kind: 'comment_ticket',
  summary: 'Comment on Fix webhook retry',
  expires_at: inMinutes(14),
  targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change' }],
  params: { ticket_id: 'tk_1', body: 'Looks good to me.' },
};

function record(a: TurnAction, patch: Partial<HelperAction>): HelperAction {
  return { ...a, status: 'pending', params: {}, result: {}, created_at: null, decided_at: null, ...patch };
}

const client = {
  getHelperAction: vi.fn(),
  confirmHelperAction: vi.fn(),
  cancelHelperAction: vi.fn(),
};

function renderCard(action: TurnAction, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HelperActionCard action={action} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ auth: { client } });
});

describe('HelperActionCard', () => {
  it('labels a project skipped by the plan gate', () => {
    renderCard({
      ...NARRATIVE,
      targets: [
        ...NARRATIVE.targets,
        { id: 'proj_3', name: 'Free repo', change: 'skipped', reason: 'upgrade_required' },
      ],
    });
    const list = screen.getByRole('list', { name: /Targets of/ });
    expect(within(list).getByText(/Needs a plan upgrade/)).toBeInTheDocument();
  });

  it('renders a pending proposal from the stored preview', () => {
    renderCard(TRASH);
    expect(screen.getByRole('heading', { name: 'Move 2 sessions to Trash' })).toBeInTheDocument();
    expect(screen.getByText('Awaiting confirmation')).toBeInTheDocument();
    expect(screen.getByText('2 will change · 1 skipped')).toBeInTheDocument();
    const list = screen.getByRole('list', { name: /Targets of/ });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    expect(within(list).getByText(/claude-code · deepseek-v4-pro · Oct 1, 2026/)).toBeInTheDocument();
    expect(within(list).getByText(/Already in Trash/)).toBeInTheDocument();
    expect(screen.getByText(/Expires in 1[34] min/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move to Trash' })).toBeInTheDocument();
    expect(client.getHelperAction).not.toHaveBeenCalled();
  });

  it('confirm calls the endpoint, shows per-target results and invalidates caches', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(TRASH, {
        status: 'confirmed',
        result: { targets: [{ id: 'ses_a', ok: true, error: null }, { id: 'ses_b', ok: false, error: 'not_owned' }] },
      }),
    );
    const { qc } = renderCard(TRASH);
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await userEvent.click(screen.getByRole('button', { name: 'Move to Trash' }));
    expect(client.confirmHelperAction).toHaveBeenCalledWith(ID, undefined);
    expect(await screen.findByText('Partly applied')).toBeInTheDocument();
    expect(screen.getByText('In Trash')).toBeInTheDocument();
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText(/Not your session/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Trash' })).toHaveAttribute('href', '/?view=trash');
    expect(screen.queryByRole('button', { name: 'Move to Trash' })).not.toBeInTheDocument();
    const keys = invalidate.mock.calls.map((c) => (c[0] as { queryKey: string[] }).queryKey[0]);
    expect(keys).toEqual(expect.arrayContaining(['projects', 'project', 'sessions']));
  });

  it('cancel calls the endpoint and shows the cancelled state', async () => {
    client.cancelHelperAction.mockResolvedValue(record(NARRATIVE, { status: 'cancelled' }));
    renderCard(NARRATIVE);
    expect(screen.getByRole('heading', { name: 'Turn Auto Narrative on for 1 project' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(client.cancelHelperAction).toHaveBeenCalledWith(ID);
    expect(await screen.findByText('Cancelled — nothing was changed.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('a 409 action_expired on confirm shows the expired state', async () => {
    client.confirmHelperAction.mockRejectedValue(
      new ApiError(409, JSON.stringify({ error: { code: 'action_expired', message: 'expired' } })),
    );
    renderCard(NARRATIVE);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText('Expired')).toBeInTheDocument();
    expect(screen.getByText(/Expired before it was confirmed/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('other errors show an alert with retry', async () => {
    client.confirmHelperAction
      .mockRejectedValueOnce(new ApiError(500, 'boom'))
      .mockResolvedValueOnce(
        record(NARRATIVE, { status: 'confirmed', result: { targets: [{ id: 'proj_1', ok: true, error: null }] } }),
      );
    renderCard(NARRATIVE);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Couldn’t apply the change')).toBeInTheDocument();
    await userEvent.click(within(alert).getByRole('button', { name: /Retry/ }));
    expect(await screen.findByText('Done')).toBeInTheDocument();
    expect(client.confirmHelperAction).toHaveBeenCalledTimes(2);
  });

  it('a card restored from history reads its status from the server', async () => {
    client.getHelperAction.mockResolvedValue(
      record(NARRATIVE, { status: 'expired', params: { project_ids: ['proj_1'], enabled: true } }),
    );
    renderCard({ id: ID, kind: 'set_auto_narrative', summary: '', targets: [], expires_at: null, fromHistory: true });
    expect(screen.getByText(/Checking this action/)).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Turn Auto Narrative on for 1 project' })).toBeInTheDocument();
    expect(client.getHelperAction).toHaveBeenCalledWith(ID);
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('a pending card whose clock ran out shows as expired', () => {
    renderCard({ ...NARRATIVE, expires_at: inMinutes(-1) });
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('has no axe violations pending and after confirm', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(TRASH, { status: 'confirmed', result: { targets: [{ id: 'ses_a', ok: true, error: null }, { id: 'ses_b', ok: true, error: null }] } }),
    );
    const { container } = renderCard(TRASH);
    expect(await axe(container)).toHaveNoViolations();
    await userEvent.click(screen.getByRole('button', { name: 'Move to Trash' }));
    await waitFor(() => expect(screen.getByText('Done')).toBeInTheDocument());
    expect(await axe(container)).toHaveNoViolations();
  });

  it('seeds the create-ticket editor from the stored draft', () => {
    renderCard(CREATE_TICKET);
    expect(screen.getByRole('heading', { name: 'Create a task ticket' })).toBeInTheDocument();
    expect(screen.getByLabelText('Title')).toHaveValue('Fix webhook retry');
    expect(screen.getByLabelText('Description')).toHaveValue('Retry with backoff.');
    expect(screen.getByLabelText('Acceptance criteria')).toHaveValue('Retries 3 times\nLogs failures');
    expect(screen.getByLabelText('Assignee')).toHaveValue('Atlas');
    expect(screen.getByText('high')).toBeInTheDocument();
  });

  it('confirm sends the edited create-ticket body and links the new ticket', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(CREATE_TICKET, {
        status: 'confirmed',
        result: { targets: [{ id: 'proj_1', ok: true, error: null, ticket_id: 'tk_0123456789abcdef' }] },
      }),
    );
    renderCard(CREATE_TICKET);
    const title = screen.getByLabelText('Title');
    await userEvent.clear(title);
    await userEvent.type(title, 'Add retry with exponential backoff');
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(client.confirmHelperAction).toHaveBeenCalledWith(ID, {
      title: 'Add retry with exponential backoff',
      description: 'Retry with backoff.',
      acceptance_criteria: ['Retries 3 times', 'Logs failures'],
      priority: 'high',
    });
    expect(await screen.findByText('Created')).toBeInTheDocument();
    expect(screen.getByText(/tk_0123456789abcdef/)).toBeInTheDocument();
  });

  it('sends null when the drafted assignee is cleared', async () => {
    client.confirmHelperAction.mockResolvedValue(record(CREATE_TICKET, { status: 'confirmed' }));
    renderCard(CREATE_TICKET);
    await userEvent.clear(screen.getByLabelText('Assignee'));
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(client.confirmHelperAction).toHaveBeenCalledWith(ID, expect.objectContaining({ assigned_to: null }));
  });

  it('sends a changed assignee and omits a blank one with no draft', async () => {
    client.confirmHelperAction.mockResolvedValue(record(CREATE_TICKET, { status: 'confirmed' }));
    const { unmount } = renderCard(CREATE_TICKET);
    const assignee = screen.getByLabelText('Assignee');
    await userEvent.clear(assignee);
    await userEvent.type(assignee, '  Scribe ');
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(client.confirmHelperAction).toHaveBeenLastCalledWith(ID, expect.objectContaining({ assigned_to: 'Scribe' }));
    unmount();

    renderCard({ ...CREATE_TICKET, params: { ...CREATE_TICKET.params, assigned_to: null } });
    await userEvent.type(screen.getByLabelText('Assignee'), '   ');
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(client.confirmHelperAction.mock.lastCall?.[1]).not.toHaveProperty('assigned_to');
  });

  it('uses "an" before a kind that starts with a vowel', () => {
    renderCard({ ...CREATE_TICKET, params: { ...CREATE_TICKET.params, kind: 'issue' } });
    expect(screen.getByRole('heading', { name: 'Create an issue ticket' })).toBeInTheDocument();
  });

  it('shows the assign persona without an editor', () => {
    renderCard(ASSIGN_TICKET);
    expect(screen.getByRole('heading', { name: 'Assign to Atlas' })).toBeInTheDocument();
    const list = screen.getByRole('list', { name: /Targets of/ });
    expect(within(list).getByText(/unassigned → Atlas/)).toBeInTheDocument();
    expect(within(list).getByText(/Already Atlas/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Comment')).not.toBeInTheDocument();
  });

  it('edits and posts the comment body', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(COMMENT_TICKET, {
        status: 'confirmed',
        result: { targets: [{ id: 'tk_1', ok: true, error: null, comment_id: 'tc_1' }] },
      }),
    );
    renderCard(COMMENT_TICKET);
    expect(screen.getByRole('heading', { name: 'Comment on Fix webhook retry' })).toBeInTheDocument();
    expect(screen.getByLabelText('Comment')).toHaveValue('Looks good to me.');
    const body = screen.getByLabelText('Comment');
    await userEvent.clear(body);
    await userEvent.type(body, 'Tested and it works.');
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(client.confirmHelperAction).toHaveBeenCalledWith(ID, { body: 'Tested and it works.' });
    expect(await screen.findByText('Commented')).toBeInTheDocument();
  });

  it('explains a comment refused because the ticket moved', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(COMMENT_TICKET, {
        status: 'failed',
        result: { targets: [{ id: 'tk_1', ok: false, error: 'ticket_moved' }] },
      }),
    );
    renderCard(COMMENT_TICKET);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText(/The ticket moved to another project/)).toBeInTheDocument();
  });

  it('has no axe violations for the create-ticket editor', async () => {
    const { container } = renderCard(CREATE_TICKET);
    expect(await axe(container)).toHaveNoViolations();
  });
});
