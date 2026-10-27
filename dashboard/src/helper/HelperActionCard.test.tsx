import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type HelperAction } from '../api/client';
import { axe } from '../test/axe';
import { actionKindForTool, actionTitle, assignedPerson, proposedActionId, reasonLabel, type TurnAction } from './actions';

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

const LINK_JIRA: TurnAction = {
  id: ID,
  kind: 'link_jira',
  summary: 'Link Fix webhook retry to EMMS-291',
  expires_at: inMinutes(14),
  targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change' }],
  params: { ticket_id: 'tk_1', issue_key: 'EMMS-291' },
};

const OVERWRITE_TO_JIRA: TurnAction = {
  id: ID,
  kind: 'jira_overwrite',
  summary: 'Replace EMMS-291 with Fix webhook retry',
  expires_at: inMinutes(14),
  targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change' }],
  params: { ticket_id: 'tk_0123456789abcdef', issue_key: 'EMMS-291', direction: 'to_jira' },
};

const OVERWRITE_FROM_JIRA: TurnAction = {
  ...OVERWRITE_TO_JIRA,
  params: { ...OVERWRITE_TO_JIRA.params, direction: 'from_jira' },
};

const CREATE_AND_PUBLISH: TurnAction = {
  ...CREATE_TICKET,
  params: { ...CREATE_TICKET.params, publish_to_jira: true, jira_project_key: 'EMMS' },
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

  it('says when a comment will also be posted to Jira', () => {
    renderCard({
      ...COMMENT_TICKET,
      params: { ...COMMENT_TICKET.params, send_to_jira: true, issue_key: 'EMMS-291' },
    });
    expect(
      screen.getByRole('heading', { name: 'Comment on Fix webhook retry and post it to Jira EMMS-291' }),
    ).toBeInTheDocument();
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

  it('titles an assignment to a person with the ticket and the person', () => {
    const toPerson: TurnAction = {
      ...ASSIGN_TICKET,
      targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change', before: 'Atlas', after: 'u_42' }],
      params: { ticket_id: 'tk_1', assigned_user_id: 'u_42', assignee_name: 'Sam Rivera' },
    };
    renderCard(toPerson);
    expect(screen.getByRole('heading', { name: 'Assign Fix webhook retry to Sam Rivera' })).toBeInTheDocument();
    const list = screen.getByRole('list', { name: /Targets of/ });
    // The person's name, never the raw user id.
    expect(within(list).getByText(/Atlas → Sam Rivera/)).toBeInTheDocument();
    expect(within(list).queryByText(/u_42/)).toBeNull();
  });

  it('explains a person who cannot be assigned', async () => {
    const toPerson: TurnAction = {
      ...ASSIGN_TICKET,
      targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change', before: null, after: 'u_42' }],
      params: { ticket_id: 'tk_1', assigned_user_id: 'u_42', assignee_name: 'Sam Rivera' },
    };
    client.confirmHelperAction.mockResolvedValue(
      record(toPerson, {
        status: 'failed',
        result: { targets: [{ id: 'tk_1', ok: false, error: 'assignee_not_eligible' }] },
      }),
    );
    renderCard(toPerson);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText(/That person can't be assigned tickets in this project/)).toBeInTheDocument();
  });

  it('shows a drafted person read-only on a create-ticket card and keeps it on confirm', async () => {
    const forPerson: TurnAction = {
      ...CREATE_TICKET,
      params: { ...CREATE_TICKET.params, assigned_to: null, assigned_user_id: 'u_42', assignee_name: 'Sam Rivera' },
    };
    client.confirmHelperAction.mockResolvedValue(record(forPerson, { status: 'confirmed' }));
    const { container } = renderCard(forPerson);
    const drafted = screen.getByTestId('drafted-assignee');
    expect(within(drafted).getByRole('note', { name: 'Assignee' })).toHaveTextContent('Sam Rivera');
    expect(drafted).toHaveTextContent(/reassign the ticket after it’s created/);
    expect(screen.queryByPlaceholderText('Persona name (optional)')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(client.confirmHelperAction).toHaveBeenCalled());
    const body = client.confirmHelperAction.mock.lastCall?.[1];
    expect(body).not.toHaveProperty('assigned_to');
    expect(body).not.toHaveProperty('assigned_user_id');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('has no axe violations for the create-ticket editor', async () => {
    const { container } = renderCard(CREATE_TICKET);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('says when a created ticket will also be published to Jira', () => {
    renderCard(CREATE_AND_PUBLISH);
    expect(
      screen.getByRole('heading', { name: 'Create a task ticket and publish it to Jira EMMS' }),
    ).toBeInTheDocument();
  });

  it('links the Jira issue a created ticket was published as', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(CREATE_AND_PUBLISH, {
        status: 'confirmed',
        result: {
          targets: [
            {
              id: 'proj_1',
              ok: true,
              error: null,
              ticket_id: 'tk_0123456789abcdef',
              jira: { issue_key: 'EMMS-300', issue_url: 'https://acme.atlassian.net/browse/EMMS-300' },
            },
          ],
        },
      }),
    );
    renderCard(CREATE_AND_PUBLISH);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    const link = await screen.findByRole('link', { name: /EMMS-300/ });
    expect(link).toHaveAttribute('href', 'https://acme.atlassian.net/browse/EMMS-300');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText(/Published to Jira as/)).toBeInTheDocument();
  });

  it('says when a created ticket was not published to Jira, and why', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(CREATE_AND_PUBLISH, {
        status: 'confirmed',
        result: {
          targets: [
            {
              id: 'proj_1',
              ok: true,
              error: null,
              ticket_id: 'tk_0123456789abcdef',
              jira_published: false,
              jira_error: 'jira_timeout',
            },
          ],
        },
      }),
    );
    renderCard(CREATE_AND_PUBLISH);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(
      await screen.findByText('Created but not published to Jira: Jira didn’t respond in time.'),
    ).toBeInTheDocument();
    // The next step, with the new ticket to publish from.
    expect(screen.getByText(/Publish it from the ticket page/)).toBeInTheDocument();
    expect(screen.getByText(/tk_0123456789abcdef/)).toBeInTheDocument();
    expect(screen.getByText('Created')).toBeInTheDocument();
    expect(screen.queryByText(/Published to Jira as/)).not.toBeInTheDocument();
  });

  it('shows a Jira link proposal without an editor and the linked issue after confirm', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(LINK_JIRA, {
        status: 'confirmed',
        result: {
          targets: [
            {
              id: 'tk_1',
              ok: true,
              error: null,
              issue_key: 'EMMS-291',
              issue_url: 'https://acme.atlassian.net/browse/EMMS-291',
            },
          ],
        },
      }),
    );
    renderCard(LINK_JIRA);
    expect(screen.getByRole('heading', { name: 'Link Fix webhook retry to Jira EMMS-291' })).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: /Targets of/ })).getByText('→ EMMS-291')).toBeInTheDocument();
    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Comment')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(client.confirmHelperAction).toHaveBeenCalledWith(ID, undefined);
    expect(await screen.findByText('Linked')).toBeInTheDocument();
    expect(screen.getByText('Applied to 1 ticket.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /EMMS-291/ })).toHaveAttribute(
      'href',
      'https://acme.atlassian.net/browse/EMMS-291',
    );
  });

  it('renders a non-http issue URL as plain text', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(LINK_JIRA, {
        status: 'confirmed',
        result: { targets: [{ id: 'tk_1', ok: true, error: null, issue_key: 'EMMS-291', issue_url: 'javascript:alert(1)' }] },
      }),
    );
    renderCard(LINK_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await screen.findByText('EMMS-291')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /EMMS-291/ })).not.toBeInTheDocument();
  });

  it('explains a Jira link refused because the issue is linked elsewhere', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(LINK_JIRA, {
        status: 'failed',
        result: { targets: [{ id: 'tk_1', ok: false, error: 'jira_issue_linked_elsewhere' }] },
      }),
    );
    renderCard(LINK_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(
      await screen.findByText('Not applied: That Jira issue is already linked to another ticket.'),
    ).toBeInTheDocument();
  });

  it('has no axe violations for a confirmed Jira link', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(LINK_JIRA, {
        status: 'confirmed',
        result: { targets: [{ id: 'tk_1', ok: true, error: null, issue_key: 'EMMS-291', issue_url: 'https://acme.atlassian.net/browse/EMMS-291' }] },
      }),
    );
    const { container } = renderCard(LINK_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await screen.findByText('Linked');
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Jira overwrite cards', () => {
  it('titles each direction and confirms with a destructive Replace', () => {
    const { unmount } = renderCard(OVERWRITE_TO_JIRA);
    expect(screen.getByRole('heading', { name: 'Replace Jira EMMS-291 with Fix webhook retry' })).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: /Targets of/ })).getByText('ticket → EMMS-291')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Replace' })).toBeInTheDocument();
    unmount();

    renderCard(OVERWRITE_FROM_JIRA);
    expect(screen.getByRole('heading', { name: 'Replace Fix webhook retry with Jira EMMS-291' })).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: /Targets of/ })).getByText('EMMS-291 → ticket')).toBeInTheDocument();
  });

  it('links the replaced Jira issue after a to_jira confirm', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(OVERWRITE_TO_JIRA, {
        status: 'confirmed',
        params: OVERWRITE_TO_JIRA.params,
        result: {
          targets: [
            { id: 'tk_1', ok: true, error: null, jira_published: true, issue_key: 'EMMS-291', issue_url: 'https://acme.atlassian.net/browse/EMMS-291' },
          ],
        },
      }),
    );
    renderCard(OVERWRITE_TO_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }));
    expect(await screen.findByText('Replaced')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /EMMS-291/ })).toHaveAttribute('href', 'https://acme.atlassian.net/browse/EMMS-291');
    expect(screen.getByText(/with this ticket\./)).toBeInTheDocument();
  });

  it('says what each direction overwrites before confirming', () => {
    const { unmount } = renderCard(OVERWRITE_TO_JIRA);
    expect(
      screen.getByText(
        'Overwrites EMMS-291’s summary and description in Jira with this ticket (default template). The Jira text isn’t kept.',
      ),
    ).toBeInTheDocument();
    unmount();
    renderCard(OVERWRITE_FROM_JIRA);
    expect(
      screen.getByText('Overwrites this ticket’s title and description with EMMS-291’s. Other fields are kept.'),
    ).toBeInTheDocument();
  });

  it.each([
    ['jira_upstream_error', 'Not applied: Jira returned an error.'],
    ['jira_link_changed', 'Not applied: The ticket is now linked to a different Jira issue.'],
    ['jira_overwrite_required', 'Not applied: Replacing the Jira issue needs an explicit choice.'],
    // A timeout may have reached Jira: never claim nothing changed.
    ['jira_timeout', 'Jira didn’t confirm the update; check EMMS-291 in Jira.'],
  ])('explains a refused to_jira replace (%s)', async (code, text) => {
    client.confirmHelperAction.mockResolvedValue(
      record(OVERWRITE_TO_JIRA, {
        status: 'failed',
        params: OVERWRITE_TO_JIRA.params,
        result: { targets: [{ id: 'tk_1', ok: false, error: code }] },
      }),
    );
    renderCard(OVERWRITE_TO_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/every target failed its access check/)).not.toBeInTheDocument();
  });

  it('a from_jira timeout is reported as not applied', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(OVERWRITE_FROM_JIRA, {
        status: 'failed',
        params: OVERWRITE_FROM_JIRA.params,
        result: { targets: [{ id: 'tk_1', ok: false, error: 'jira_timeout' }] },
      }),
    );
    renderCard(OVERWRITE_FROM_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }));
    expect(await screen.findByText('Not applied: Jira didn’t respond in time.')).toBeInTheDocument();
  });

  it.each([
    [['title', 'description'], 'Updated the title and description from Jira EMMS-291.'],
    [[], 'Already matched Jira EMMS-291 — nothing changed.'],
  ])('reports a from_jira pull (%j)', async (fields, text) => {
    client.confirmHelperAction.mockResolvedValue(
      record(OVERWRITE_FROM_JIRA, {
        status: 'confirmed',
        params: OVERWRITE_FROM_JIRA.params,
        result: { targets: [{ id: 'tk_1', ok: true, error: null, issue_key: 'EMMS-291', updated_fields: fields }] },
      }),
    );
    renderCard(OVERWRITE_FROM_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('explains a refused overwrite', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(OVERWRITE_FROM_JIRA, {
        status: 'failed',
        result: { targets: [{ id: 'tk_1', ok: false, error: 'jira_not_linked' }] },
      }),
    );
    renderCard(OVERWRITE_FROM_JIRA);
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }));
    expect(await screen.findByText('Not applied: The ticket isn’t linked to a Jira issue.')).toBeInTheDocument();
  });

  it('has no axe violations pending and confirmed', async () => {
    client.confirmHelperAction.mockResolvedValue(
      record(OVERWRITE_FROM_JIRA, {
        status: 'confirmed',
        params: OVERWRITE_FROM_JIRA.params,
        result: { targets: [{ id: 'tk_1', ok: true, error: null, issue_key: 'EMMS-291', updated_fields: ['title'] }] },
      }),
    );
    const { container } = renderCard(OVERWRITE_FROM_JIRA);
    expect(await axe(container)).toHaveNoViolations();
    await userEvent.click(screen.getByRole('button', { name: 'Replace' }));
    await screen.findByText('Replaced');
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Jira helper action labels', () => {
  it('maps propose_jira_overwrite to jira_overwrite and recognises its stored rows', () => {
    expect(actionKindForTool('propose_jira_overwrite')).toBe('jira_overwrite');
    expect(
      proposedActionId({ tool_name: 'propose_jira_overwrite', content: `Proposed action ${ID} to replace EMMS-291` }),
    ).toBe(ID);
  });

  it('falls back when an overwrite preview has no ticket name or key', () => {
    const base = { kind: 'jira_overwrite', summary: '', targets: [{ id: 'tk_1', name: '', change: 'will_change' as const }] };
    expect(actionTitle({ ...base, params: { direction: 'to_jira' } })).toBe('Replace the linked Jira issue with the ticket');
    expect(actionTitle({ ...base, params: { direction: 'from_jira' } })).toBe('Replace the ticket with the linked Jira issue');
  });

  it('maps propose_jira_link to link_jira and recognises its stored rows', () => {
    expect(actionKindForTool('propose_jira_link')).toBe('link_jira');
    expect(
      proposedActionId({ tool_name: 'propose_jira_link', content: `Proposed action ${ID} to link EMMS-291` }),
    ).toBe(ID);
  });

  it('falls back when the link preview has no ticket name or key', () => {
    expect(
      actionTitle({ kind: 'link_jira', summary: '', targets: [{ id: 'tk_1', name: '', change: 'will_change' }], params: {} }),
    ).toBe('Link ticket to a Jira issue');
  });

  it('leaves the Jira project out of the create title when the proposal has none', () => {
    expect(
      actionTitle({ kind: 'create_ticket', summary: '', targets: [], params: { kind: 'issue', publish_to_jira: true } }),
    ).toBe('Create an issue ticket and publish it to Jira');
  });

  it.each([
    ['jira_already_linked', 'The ticket is already linked to a Jira issue'],
    ['jira_issue_not_found', 'No such Jira issue, or the Jira connection can’t see it'],
    ['jira_project_not_mapped', 'That Jira project isn’t mapped to this project'],
    ['jira_issue_linked_elsewhere', 'That Jira issue is already linked to another ticket'],
    ['jira_timeout', 'Jira didn’t respond in time'],
    ['jira_upstream_error', 'Jira returned an error'],
    ['jira_edit_required', 'Jira actions need edit access to this project'],
    ['content_too_large', 'Some content is too large to publish to Jira Cloud'],
    ['content_too_large_for_jira', 'Some content is too large to publish to Jira Cloud'],
    ['jira_adopt_rejected', 'A Jira issue from an earlier publish couldn’t be confirmed as this ticket’s'],
    ['publish_superseded', 'A newer publish superseded this one'],
    ['link_changed', 'The ticket’s Jira link changed while publishing'],
    ['publish_in_progress', 'Another publish for this ticket is already in progress'],
    ['jira_connection_failed', 'Jira rejected the stored connection; an admin should re-save it'],
    ['invalid_issue_key', 'That isn’t a valid Jira issue key'],
    ['error', 'Something went wrong'],
    ['jira_overwrite_required', 'Replacing the Jira issue needs an explicit choice'],
    ['jira_not_linked', 'The ticket isn’t linked to a Jira issue'],
  ])('labels %s', (code, label) => {
    expect(reasonLabel(code)).toBe(label);
  });
});

describe('assign_ticket helpers', () => {
  it('builds titles for person and persona assignments', () => {
    const base = { kind: 'assign_ticket', summary: '', targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change' as const }] };
    expect(actionTitle({ ...base, params: { ticket_id: 'tk_1', assigned_user_id: 'u1', assignee_name: 'Ola Dev' } })).toBe(
      'Assign Fix webhook retry to Ola Dev',
    );
    expect(actionTitle({ ...base, targets: [], params: { ticket_id: 'tk_9', assigned_user_id: 'u1' } })).toBe(
      'Assign tk_9 to a person',
    );
    expect(actionTitle({ ...base, params: { ticket_id: 'tk_1', assigned_to: 'atlas' } })).toBe('Assign to atlas');
  });

  it('reads the person out of params only when a user id is present', () => {
    expect(assignedPerson({ assigned_user_id: 'u1', assignee_name: ' Ola ' })).toEqual({ id: 'u1', name: 'Ola' });
    expect(assignedPerson({ assigned_to: 'atlas' })).toBeNull();
    // Proposals stored before the param rename.
    expect(assignedPerson({ assigned_user_id: 'u1', assigned_user_display_name: 'Ola Dev' })).toEqual({ id: 'u1', name: 'Ola Dev' });
    expect(assignedPerson({ assigned_user_id: 'u1', assignee_name: 'New', assigned_user_display_name: 'Old' })).toEqual({ id: 'u1', name: 'New' });
    expect(assignedPerson(undefined)).toBeNull();
  });

  it('labels the assignee reason codes', () => {
    expect(reasonLabel('assignee_not_eligible')).toBe("That person can't be assigned tickets in this project");
    expect(reasonLabel('assignee_conflict')).toBe('A ticket can have a person or a persona as assignee, not both');
  });
});
