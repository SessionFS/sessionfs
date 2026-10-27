import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TicketsTab from './TicketsTab';
import { ApiError } from '../api/client';
import { axe } from '../test/axe';

const { hooks, mockAddToast, mockUseMe, mockUseJiraLink, mockUsePersonas } = vi.hoisted(() => ({
  mockUseJiraLink: vi.fn(),
  mockUsePersonas: vi.fn(),
  hooks: {
    useTickets: vi.fn(),
    useTicket: vi.fn(),
    useTicketChildren: vi.fn(),
    useTicketComments: vi.fn(),
    useTicketJiraSyncEvents: vi.fn(),
    useTicketReviewState: vi.fn(),
    useCreateTicket: vi.fn(),
    useApproveTicket: vi.fn(),
    useDismissTicket: vi.fn(),
    useCloseTicket: vi.fn(),
    useAddTicketComment: vi.fn(),
    useTicketWatchers: vi.fn(),
    useWatchTicket: vi.fn(),
    useUnwatchTicket: vi.fn(),
    useUpdateTicket: vi.fn(),
    useAssignableUsers: vi.fn(),
  },
  mockAddToast: vi.fn(),
  mockUseMe: vi.fn(),
}));

vi.mock('../hooks/useTickets', () => hooks);
vi.mock('../hooks/useJiraTickets', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useJiraTickets')>()),
  useJiraLink: mockUseJiraLink,
}));
vi.mock('../hooks/useMe', () => ({
  useMe: mockUseMe,
}));
vi.mock('../hooks/usePersonas', () => ({
  usePersonas: mockUsePersonas,
}));
vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
vi.mock('../hooks/useFocusTrap', () => ({
  useFocusTrap: vi.fn(),
}));

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
    isError: false,
    error: null,
    ...extra,
  };
}

function ticket(overrides: Record<string, unknown> = {}) {
  return {
    id: 'tk_1',
    project_id: 'proj_1',
    title: 'Add session export',
    description: 'Implement export endpoint.',
    priority: 'medium',
    assigned_to: 'atlas',
    created_by_user_id: 'u1',
    created_by_session_id: null,
    created_by_persona: null,
    status: 'suggested',
    kind: 'task',
    parent_ticket_id: null,
    child_ticket_ids: [],
    context_refs: [],
    file_refs: [],
    related_sessions: [],
    acceptance_criteria: ['CLI works', 'Tests added'],
    resolver_session_id: null,
    resolver_user_id: null,
    completion_notes: null,
    changed_files: [],
    knowledge_entry_ids: [],
    depends_on: [],
    created_at: '2026-05-14T00:00:00Z',
    updated_at: '2026-05-14T01:00:00Z',
    resolved_at: null,
    ...overrides,
  };
}

const ASSIGNABLE = [
  { id: 'u1', display_name: 'Ola Dev', email: 'ola@example.test', can_edit: true },
  { id: 'u2', display_name: 'Sam Rivera', email: 'sam@example.test', can_edit: true },
  { id: 'u3', display_name: null, email: 'kim@example.test', can_edit: false },
];

function persona(overrides: Record<string, unknown> = {}) {
  return {
    id: 'pers_1', project_id: 'proj_1', org_id: null, scope: 'project', name: 'atlas', role: 'Backend engineer',
    content: '', specializations: [], is_active: true, version: 1, created_by: 'u1', represented_user_id: null,
    created_at: '2026-05-14T00:00:00Z', updated_at: '2026-05-14T00:00:00Z',
    ...overrides,
  };
}

const PERSONAS = [
  persona(),
  persona({ id: 'pers_2', project_id: null, org_id: 'org_1', scope: 'org', name: 'sentinel', role: 'Security reviewer', represented_user_id: 'u2' }),
  persona({ id: 'pers_3', name: 'retired', is_active: false }),
];

function watcher(overrides: Record<string, unknown> = {}) {
  return {
    user_id: 'u1',
    persona_name: null,
    source: 'explicit',
    muted: false,
    created_at: '2026-05-14T01:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  hooks.useTickets.mockReturnValue({ data: [ticket()], isLoading: false, error: null });
  hooks.useTicket.mockReturnValue({ data: undefined });
  hooks.useTicketChildren.mockReturnValue([]);
  hooks.useTicketComments.mockReturnValue({ data: [] });
  hooks.useTicketJiraSyncEvents.mockReturnValue({ data: { events: [] } });
  hooks.useTicketReviewState.mockReturnValue({ data: undefined });
  hooks.useCreateTicket.mockReturnValue(makeMutation());
  hooks.useApproveTicket.mockReturnValue(makeMutation());
  hooks.useDismissTicket.mockReturnValue(makeMutation());
  hooks.useCloseTicket.mockReturnValue(makeMutation());
  hooks.useAddTicketComment.mockReturnValue(makeMutation());
  hooks.useTicketWatchers.mockReturnValue({ data: [] });
  hooks.useWatchTicket.mockReturnValue(makeMutation());
  hooks.useUnwatchTicket.mockReturnValue(makeMutation());
  hooks.useUpdateTicket.mockReturnValue(makeMutation());
  hooks.useAssignableUsers.mockReturnValue({ data: ASSIGNABLE, isLoading: false });
  mockUsePersonas.mockReturnValue({ data: PERSONAS, isLoading: false });
  mockUseMe.mockReturnValue({ data: { user_id: 'u1' }, isLoading: false });
  mockUseJiraLink.mockReturnValue({ data: undefined, refetch: vi.fn() });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('TicketsTab', () => {
  it('renders ticket rows with status badge', () => {
    render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByText('Add session export')).toBeInTheDocument();
    expect(screen.getByText('suggested')).toBeInTheDocument();
    expect(screen.getByText(/2 criteria/)).toBeInTheDocument();
  });

  it('filters by status', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    await user.click(screen.getByRole('button', { name: /Filter by status/i }));
    await user.click(
      screen.getByRole('option', { name: 'Review' }),
    );
    expect(hooks.useTickets).toHaveBeenLastCalledWith('proj_1', { status: 'review' });
  });

  it('expands ticket detail on click', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByText('Implement export endpoint.')).toBeInTheDocument();
    expect(screen.getByText(/CLI works/)).toBeInTheDocument();
  });

  it('status action is independent of row select — opening it does not expand the row (R2)', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    // The collapsed row has exactly two buttons: the expand trigger (carries
    // aria-expanded) and the status action (a sibling, not nested in it).
    const row = screen.getByText('Add session export').closest('li') as HTMLElement;
    const statusBtn = within(row)
      .getAllByRole('button')
      .find((b) => !b.hasAttribute('aria-expanded'));
    expect(statusBtn).toBeTruthy();

    await user.click(statusBtn!);
    // The status menu opened (an action is offered) ...
    expect(screen.getByRole('menuitem', { name: /Approve/i })).toBeInTheDocument();
    // ... and the row did NOT expand (detail body stays absent).
    expect(screen.queryByText('Implement export endpoint.')).toBeNull();
  });

  it('shows approve + dismiss buttons for a suggested ticket', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByRole('button', { name: /Approve/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Dismiss/i })).toBeInTheDocument();
  });

  it('hides approve once the ticket is open', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ status: 'open' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.queryByRole('button', { name: /Approve/i })).toBeNull();
    expect(screen.getByRole('button', { name: /Dismiss/i })).toBeInTheDocument();
  });

  it('opens the new ticket modal', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New ticket/i }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /New ticket/i })).toBeInTheDocument();
  });

  it('filters by kind', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    await user.click(screen.getByRole('button', { name: /Filter by kind/i }));
    await user.click(
      screen.getByRole('option', { name: 'Issues' }),
    );
    expect(hooks.useTickets).toHaveBeenLastCalledWith('proj_1', {
      status: undefined,
      kind: 'issue',
    });
  });

  it('renders a kind badge on the row', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ kind: 'issue', status: 'open' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByText('Issue')).toBeInTheDocument();
  });

  it('shows the Issues empty-state explainer when kind=Issues filter has no rows', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<TicketsTab projectId="proj_1" />);
    await user.click(screen.getByRole('button', { name: /Filter by kind/i }));
    await user.click(
      screen.getByRole('option', { name: 'Issues' }),
    );
    expect(screen.getByText(/No Issues yet/i)).toBeInTheDocument();
    expect(screen.getByText(/PM-triaged container/i)).toBeInTheDocument();
  });

  it('renders the Children rollup with clickable child links on an expanded Issue', () => {
    const issue = ticket({
      id: 'tk_issue',
      title: 'CORS preflight rejects If-Match',
      kind: 'issue',
      status: 'in_progress',
      child_ticket_ids: ['tk_child_a', 'tk_child_b'],
    });
    hooks.useTickets.mockReturnValue({ data: [issue], isLoading: false, error: null });
    hooks.useTicketChildren.mockReturnValue([
      {
        data: ticket({
          id: 'tk_child_a',
          title: 'Sentinel header audit',
          status: 'review',
          assigned_to: 'sentinel',
          parent_ticket_id: 'tk_issue',
        }),
        isError: false,
      },
      {
        data: ticket({
          id: 'tk_child_b',
          title: 'Dashboard kind filter',
          status: 'in_progress',
          assigned_to: 'prism',
          parent_ticket_id: 'tk_issue',
        }),
        isError: false,
      },
    ]);
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('CORS preflight rejects If-Match'));
    expect(screen.getByText(/Children \(2\)/)).toBeInTheDocument();
    expect(screen.getByText('Sentinel header audit')).toBeInTheDocument();
    expect(screen.getByText('Dashboard kind filter')).toBeInTheDocument();
  });

  it('shows a Back-to-parent breadcrumb on a Task with parent_ticket_id', () => {
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({
          id: 'tk_child',
          title: 'Implement filter',
          status: 'in_progress',
          kind: 'task',
          parent_ticket_id: 'tk_issue',
        }),
      ],
      isLoading: false,
      error: null,
    });
    // TicketDetail calls useTicket twice: once for the row detail (tk_child)
    // and once for the parent breadcrumb (tk_issue). Resolve each by id so
    // the detail falls back to the row data and the breadcrumb renders.
    hooks.useTicket.mockImplementation(
      (_projectId: string, ticketId: string | undefined) => {
        if (ticketId === 'tk_issue') {
          return {
            data: ticket({
              id: 'tk_issue',
              title: 'CORS preflight rejects If-Match',
              kind: 'issue',
              status: 'in_progress',
            }),
          };
        }
        return { data: undefined };
      },
    );
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Implement filter'));
    const breadcrumb = screen.getByRole('button', { name: /Back to parent Issue/i });
    expect(breadcrumb).toBeInTheDocument();
    expect(breadcrumb).toHaveTextContent('tk_issue');
    expect(breadcrumb).toHaveTextContent('CORS preflight rejects If-Match');
  });

  it('shows a Close button on an in_progress Issue and hides it on Tasks', () => {
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({
          id: 'tk_issue',
          title: 'CORS issue',
          kind: 'issue',
          status: 'in_progress',
        }),
      ],
      isLoading: false,
      error: null,
    });
    const { unmount } = render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('CORS issue'));
    expect(screen.getByRole('button', { name: /Close Issue/i })).toBeInTheDocument();
    unmount();

    hooks.useTickets.mockReturnValue({
      data: [
        ticket({
          id: 'tk_task',
          title: 'Plain task',
          kind: 'task',
          status: 'in_progress',
        }),
      ],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Plain task'));
    expect(screen.queryByRole('button', { name: /Close Issue/i })).toBeNull();
  });

  it('exposes a kind selector and conditional Parent Issue picker in the new ticket modal', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New ticket/i }));
    // Kind selector shows "Task" selected by default
    const kindTrigger = screen.getByRole('button', { name: 'Kind' });
    expect(kindTrigger).toHaveTextContent('Task');
    // Parent Issue picker is visible when kind is task
    expect(screen.getByRole('button', { name: /Parent Issue/ })).toBeInTheDocument();
    // Switch to Issue
    await user.click(kindTrigger);
    await user.click(
      screen.getByRole('option', { name: 'Issue' }),
    );
    // Parent Issue picker hidden for Issues
    expect(screen.queryByRole('button', { name: /Parent Issue/ })).toBeNull();
  });

  /* ── Board view drawer (phase 3 fix C2) ── */

  it('opens detail in a drawer on board card click', () => {
    localStorage.setItem('sfs-tickets-view', 'board');
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    // Detail content renders inside the drawer dialog
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Implement export endpoint.')).toBeInTheDocument();
    expect(screen.getByText(/CLI works/)).toBeInTheDocument();
  });

  it('closes the board drawer on Escape', () => {
    localStorage.setItem('sfs-tickets-view', 'board');
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('renders comment composer at full width in the board drawer', () => {
    localStorage.setItem('sfs-tickets-view', 'board');
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    // Comment textarea and button are inside the drawer
    expect(screen.getByPlaceholderText('Add comment…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Comment$/ })).toBeInTheDocument();
  });

  it('sets aria-haspopup="dialog" on board card buttons', () => {
    localStorage.setItem('sfs-tickets-view', 'board');
    render(<TicketsTab projectId="proj_1" />);
    const card = screen.getByRole('button', { name: /Add session export/ });
    expect(card).toHaveAttribute('aria-haspopup', 'dialog');
  });

  it('list view still expands in place without a dialog', () => {
    // Default view is list (no localStorage override)
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByText('Implement export endpoint.')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows approve + dismiss buttons in the board drawer', () => {
    localStorage.setItem('sfs-tickets-view', 'board');
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByRole('button', { name: /Approve/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Dismiss/i })).toBeInTheDocument();
  });

  /* ── Follow / unfollow / mute (ticket watchers) ── */

  it('renders a Follow button when the current user is not watching', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByRole('button', { name: 'Follow' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unfollow' })).toBeNull();
  });

  it('follows the ticket when the Follow button is clicked', async () => {
    const watch = makeMutation({ mutateAsync: vi.fn().mockResolvedValue({}) });
    hooks.useWatchTicket.mockReturnValue(watch);
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    await user.click(screen.getByRole('button', { name: 'Follow' }));
    expect(watch.mutateAsync).toHaveBeenCalledWith(
      { ticketId: 'tk_1', muted: false },
    );
  });

  it('shows Following + Unfollow + Mute when the current user is watching', () => {
    hooks.useTicketWatchers.mockReturnValue({ data: [watcher()] });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByText('Following')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unfollow' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Mute' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Follow' })).toBeNull();
  });

  it('shows the muted state and Unmute when watching muted', () => {
    hooks.useTicketWatchers.mockReturnValue({ data: [watcher({ muted: true })] });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByText('Following (muted)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unmute' })).toBeInTheDocument();
  });

  it('unfollows when the Unfollow button is clicked', async () => {
    hooks.useTicketWatchers.mockReturnValue({ data: [watcher()] });
    const unwatch = makeMutation({ mutateAsync: vi.fn().mockResolvedValue(undefined) });
    hooks.useUnwatchTicket.mockReturnValue(unwatch);
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    await user.click(screen.getByRole('button', { name: 'Unfollow' }));
    expect(unwatch.mutateAsync).toHaveBeenCalledWith('tk_1');
  });

  /* ── Jira comment mirroring (GitHub #98 part B) ── */

  const LINKED_JIRA = {
    issue_key: 'KEY-1',
    issue_url: 'https://acme.atlassian.net/browse/KEY-1',
    last_known_status: null,
  };

  function jiraLink(overrides: Record<string, unknown> = {}) {
    return {
      ticket_id: 'tk_1',
      provider: 'jira',
      issue_key: 'KEY-1',
      issue_url: 'https://acme.atlassian.net/browse/KEY-1',
      last_pushed_at: null,
      last_known_status: null,
      last_status_checked_at: null,
      payload_hash: null,
      comment_sync_outbound: true,
      can_send_to_jira: true,
      ...overrides,
    };
  }

  it('shows "Send to Jira" to an editor on a linked ticket and posts the flag', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: LINKED_JIRA })],
      isLoading: false,
      error: null,
    });
    mockUseJiraLink.mockReturnValue({ data: jiraLink(), refetch: vi.fn() });
    const mutateAsync = vi.fn().mockResolvedValue(undefined);
    hooks.useAddTicketComment.mockReturnValue(makeMutation({ mutateAsync }));
    const { container } = render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));

    // The link query is only enabled for a linked ticket.
    expect(mockUseJiraLink).toHaveBeenLastCalledWith('proj_1', 'tk_1', true);
    const checkbox = screen.getByRole('checkbox', { name: /send to jira/i });

    await user.type(screen.getByPlaceholderText('Add comment…'), 'Ship it');
    await user.click(checkbox);
    await user.click(screen.getByRole('button', { name: /^Comment$/ }));

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        content: 'Ship it',
        sendToJira: true,
      });
    });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('hides "Send to Jira" from a non-editor on a linked ticket and never sends the flag', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: LINKED_JIRA })],
      isLoading: false,
      error: null,
    });
    mockUseJiraLink.mockReturnValue({
      data: jiraLink({ can_send_to_jira: false }),
      refetch: vi.fn(),
    });
    const mutateAsync = vi.fn().mockResolvedValue(undefined);
    hooks.useAddTicketComment.mockReturnValue(makeMutation({ mutateAsync }));
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));

    expect(screen.queryByRole('checkbox', { name: /send to jira/i })).toBeNull();
    await user.type(screen.getByPlaceholderText('Add comment…'), 'Looks good');
    await user.click(screen.getByRole('button', { name: /^Comment$/ }));
    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        content: 'Looks good',
        sendToJira: undefined,
      });
    });
  });

  it('hides "Send to Jira" while the link has not loaded', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: LINKED_JIRA })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.queryByRole('checkbox', { name: /send to jira/i })).toBeNull();
  });

  it('does not fetch Jira sync events or the link for an unlinked ticket', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(hooks.useTicketJiraSyncEvents).toHaveBeenCalled();
    for (const call of hooks.useTicketJiraSyncEvents.mock.calls) {
      expect(call[2]).toBe(false);
    }
    for (const call of mockUseJiraLink.mock.calls) {
      expect(call[2]).toBe(false);
    }
    expect(screen.queryByRole('checkbox', { name: /send to jira/i })).toBeNull();
    expect(screen.queryByText('Jira activity')).toBeNull();
  });

  it('fetches Jira sync events for a linked ticket', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: LINKED_JIRA })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(hooks.useTicketJiraSyncEvents).toHaveBeenLastCalledWith('proj_1', 'tk_1', true);
  });

  it('explains a 409 jira_comment_sync_off, keeps the draft and refreshes the link', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: LINKED_JIRA })],
      isLoading: false,
      error: null,
    });
    const refetch = vi.fn();
    mockUseJiraLink.mockReturnValue({ data: jiraLink(), refetch });
    const mutateAsync = vi.fn().mockRejectedValue(
      new ApiError(
        409,
        JSON.stringify({
          error: {
            code: '409',
            message: 'Outbound comment sync is off',
            details: { error: 'jira_comment_sync_off' },
          },
        }),
      ),
    );
    hooks.useAddTicketComment.mockReturnValue(makeMutation({ mutateAsync }));
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));

    await user.type(screen.getByPlaceholderText('Add comment…'), 'Ship it');
    await user.click(screen.getByRole('checkbox', { name: /send to jira/i }));
    await user.click(screen.getByRole('button', { name: /^Comment$/ }));

    await waitFor(() => expect(refetch).toHaveBeenCalled());
    expect(mockAddToast).toHaveBeenCalledWith('error', expect.stringMatching(/^Comment not posted/));
    expect(screen.getByPlaceholderText('Add comment…')).toHaveValue('Ship it');
  });

  it('marks inbound Jira comments with a Jira badge', () => {
    hooks.useTicketComments.mockReturnValue({
      data: [
        {
          id: 'c_1',
          ticket_id: 'tk_1',
          author_user_id: 'u1',
          author_persona: 'atlas',
          content: 'Done upstream',
          session_id: null,
          created_at: '2026-05-14T01:00:00Z',
          source: 'jira',
        },
        {
          id: 'c_2',
          ticket_id: 'tk_1',
          author_user_id: 'u1',
          author_persona: null,
          content: 'Noted',
          session_id: null,
          created_at: '2026-05-14T02:00:00Z',
          source: 'sessionfs',
        },
      ],
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    // Exactly one comment is inbound from Jira.
    expect(screen.getByText('Done upstream')).toBeInTheDocument();
    expect(screen.getByText('Noted')).toBeInTheDocument();
    expect(screen.getAllByText('Jira')).toHaveLength(1);
  });

  it('shows Jira activity from the sync ledger in the ticket timeline', async () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: LINKED_JIRA })],
      isLoading: false,
      error: null,
    });
    hooks.useTicketJiraSyncEvents.mockReturnValue({
      data: {
        events: [
          {
            id: 'tse_1',
            ticket_id: 'tk_1',
            issue_key: 'KEY-1',
            direction: 'outbound',
            kind: 'evidence',
            payload: {},
            jira_event_id: null,
            applied: true,
            error: null,
            created_at: '2026-05-14T03:00:00Z',
          },
          {
            id: 'tse_2',
            ticket_id: 'tk_1',
            issue_key: 'KEY-1',
            direction: 'inbound',
            kind: 'comment',
            payload: {},
            jira_event_id: 'comment:c1',
            applied: true,
            error: null,
            created_at: '2026-05-14T04:00:00Z',
          },
          {
            id: 'tse_3',
            ticket_id: 'tk_1',
            issue_key: 'KEY-1',
            direction: 'outbound',
            kind: 'comment',
            payload: {},
            jira_event_id: null,
            applied: false,
            error: null,
            created_at: '2026-05-14T05:00:00Z',
            state: 'pending',
            retry_count: 0,
            next_attempt_at: null,
            updated_at: null,
          },
          {
            id: 'tse_4',
            ticket_id: 'tk_1',
            issue_key: 'KEY-1',
            direction: 'outbound',
            kind: 'status',
            payload: {},
            jira_event_id: null,
            applied: false,
            error: 'no transition "Done" available',
            created_at: '2026-05-14T06:00:00Z',
            state: 'exhausted',
            retry_count: 5,
            next_attempt_at: null,
            updated_at: '2026-05-14T07:00:00Z',
          },
        ],
      },
    });
    const { container } = render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));

    // Jira sync events sit in the one activity timeline, in time order.
    const activity = screen.getByRole('region', { name: 'Activity' });
    expect(within(activity).getByText('Completion evidence pushed')).toBeInTheDocument();
    // A mirrored Jira comment already shows as the comment itself.
    expect(screen.queryByText('Jira comment mirrored')).not.toBeInTheDocument();
    // An outbound push with no matching comment keeps its own row.
    expect(screen.getByText('Comment sent to Jira')).toBeInTheDocument();
    expect(screen.getByText('queued')).toBeInTheDocument();
    expect(screen.getByText('gave up')).toBeInTheDocument();
    expect(screen.getByText('no transition "Done" available')).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
  /* ── Calm detail: header, folded sections, one timeline ── */

  it('leads with a header of status, assignee and actions', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByText('Assigned to')).toBeInTheDocument();
    expect(screen.getAllByText('suggested').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('button', { name: /Approve/i })).toBeInTheDocument();
  });

  it('folds dependencies away and keeps completion notes open', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ depends_on: ['tk_dep'], completion_notes: 'Shipped behind a flag.' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    const deps = screen.getByText('Depends on (1)').closest('details')!;
    expect(deps).not.toHaveAttribute('open');
    expect(screen.getByText('Completion notes').closest('details')).toHaveAttribute('open');
  });

  it('merges comments, trusted verdicts, Jira events and lifecycle events into one timeline', async () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: { issue_key: 'KEY-1', issue_url: 'https://acme.atlassian.net/browse/KEY-1', last_known_status: null } })],
      isLoading: false,
      error: null,
    });
    const comment = (id: string, persona: string | null, content: string, at: string) => ({
      id, ticket_id: 'tk_1', author_user_id: 'u1', author_persona: persona, content, session_id: null, created_at: at, source: 'sessionfs',
    });
    hooks.useTicketComments.mockReturnValue({
      data: [
        comment('c1', 'codex-reviewer', 'Codex R1 review on tk_1: VERIFIED-CLEAN', '2026-05-14T04:00:00Z'),
        comment('c2', 'system', 'Edited by atlas: priority low → medium', '2026-05-14T02:00:00Z'),
        comment('c3', 'codex-reviewer', 'Looks fine to me', '2026-05-14T05:00:00Z'),
      ],
    });
    hooks.useTicketReviewState.mockReturnValue({
      data: {
        ticket_id: 'tk_1',
        review_state: {
          open_findings: [],
          closed_findings: [],
          last_verdict: 'VERIFIED-CLEAN',
          rounds: [
            { round: 1, verdict: 'VERIFIED-CLEAN', raw_verdict: 'VERIFIED-CLEAN', is_strict_verified_clean: true, comment_id: 'c1', timestamp: '2026-05-14T04:00:00Z', findings_raised: 0 },
          ],
        },
      },
    });
    hooks.useTicketJiraSyncEvents.mockReturnValue({
      data: {
        events: [
          { id: 'e1', ticket_id: 'tk_1', issue_key: 'KEY-1', direction: 'inbound', kind: 'status', payload: { jira_status: 'Done', action: 'resolve', outcome: 'applied' }, jira_event_id: null, applied: true, error: null, created_at: '2026-05-14T03:00:00Z' },
        ],
      },
    });
    const { container } = render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));

    const activity = screen.getByRole('region', { name: 'Activity' });
    const rows = within(activity).getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(rows[0]).toMatch(/^Created/);
    expect(rows[1]).toMatch(/Edited by atlas/);
    expect(rows[2]).toMatch(/Jira moved to Done \(ticket resolved\)/);
    expect(rows[3]).toMatch(/Round 1: verified clean/);
    expect(rows[3]).toMatch(/Trusted reviewer/);
    // A reviewer comment that is not a trusted round gets no verdict badge.
    expect(rows[4]).toMatch(/Looks fine to me/);
    expect(rows[4]).not.toMatch(/Trusted reviewer/);
    // The composer ends the timeline.
    expect(within(activity).getByPlaceholderText('Add comment…')).toBeInTheDocument();
    expect(hooks.useTicketReviewState).toHaveBeenCalledWith('proj_1', 'tk_1');
    expect(await axe(container)).toHaveNoViolations();
  });
  it('drops ledger rows that repeat a comment and folds outbound pushes onto their comment', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ jira: { issue_key: 'KEY-1', issue_url: 'https://acme.atlassian.net/browse/KEY-1', last_known_status: null } })],
      isLoading: false,
      error: null,
    });
    const at = (h: number) => `2026-05-14T0${h}:00:00Z`;
    hooks.useTicketComments.mockReturnValue({
      data: [
        { id: 'c_in', ticket_id: 'tk_1', author_user_id: 'u2', author_persona: null, content: 'From Jira text', session_id: null, created_at: at(2), source: 'jira' },
        { id: 'c_out', ticket_id: 'tk_1', author_user_id: 'u1', author_persona: 'atlas', content: 'Pushed comment', session_id: null, created_at: at(3), source: 'sessionfs' },
        { id: 'c_edit', ticket_id: 'tk_1', author_user_id: 'u2', author_persona: 'system', content: 'Edited in Jira by Priya: description changed', session_id: null, created_at: at(4), source: 'jira' },
      ],
    });
    const ev = (id: string, extra: Record<string, unknown>) => ({
      id, ticket_id: 'tk_1', issue_key: 'KEY-1', jira_event_id: null, applied: true, error: null, payload: {},
      state: 'done', retry_count: 0, next_attempt_at: null, updated_at: null, ...extra,
    });
    hooks.useTicketJiraSyncEvents.mockReturnValue({
      data: {
        // Newest first, as the API returns them.
        events: [
          ev('e_edit', { direction: 'inbound', kind: 'field_edit', payload: { field: 'description' }, created_at: at(4) }),
          ev('e_out', { direction: 'outbound', kind: 'comment', payload: { comment_id: 'c_out' }, created_at: at(3), applied: false, state: 'pending', retry_count: 1, error: 'Jira returned 503' }),
          ev('e_bad', { direction: 'inbound', kind: 'comment', payload: {}, created_at: at(2), applied: false, error: 'author not allowed', state: 'skipped' }),
          ev('e_in', { direction: 'inbound', kind: 'comment', payload: { comment_id: '10001' }, created_at: at(2) }),
        ],
      },
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));

    const activity = screen.getByRole('region', { name: 'Activity' });
    const rows = within(activity).getAllByRole('listitem').map((li) => li.textContent ?? '');
    // Created, the inbound comment, the skipped inbound row, the pushed comment, the edit note.
    expect(rows).toHaveLength(5);
    // The applied inbound row is dropped; the skipped one stays, with its reason.
    expect(rows.some((r) => /Jira comment mirrored/.test(r))).toBe(false);
    expect(rows.find((r) => /Jira comment not mirrored/.test(r))).toMatch(/author not allowed/);
    expect(rows.some((r) => /Jira edited description/.test(r))).toBe(false);
    const pushed = rows.find((r) => /Pushed comment/.test(r))!;
    expect(pushed).toMatch(/Jira: retrying \(attempt 2\)/);
    expect(pushed).toMatch(/Jira returned 503/);
    expect(rows.some((r) => /^Comment sent to Jira/.test(r))).toBe(false);
  });

  it('keeps same-time ledger rows in the order they happened', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ created_at: null, jira: { issue_key: 'KEY-1', issue_url: 'https://acme.atlassian.net/browse/KEY-1', last_known_status: null } })],
      isLoading: false,
      error: null,
    });
    const same = '2026-05-14T03:00:00Z';
    const ev = (id: string, payload: Record<string, unknown>) => ({
      id, ticket_id: 'tk_1', issue_key: 'KEY-1', direction: 'outbound', kind: 'status', payload,
      jira_event_id: null, applied: true, error: null, created_at: same, state: 'done', retry_count: 0, next_attempt_at: null, updated_at: null,
    });
    hooks.useTicketJiraSyncEvents.mockReturnValue({
      data: { events: [ev('e2', { to_status: 'done', transition: 'Done' }), ev('e1', { to_status: 'in_progress', transition: 'Start' })] },
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    const rows = within(screen.getByRole('region', { name: 'Activity' })).getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(rows[0]).toMatch(/Status pushed: In progress → Jira “Start”/);
    expect(rows[1]).toMatch(/Status pushed: Done → Jira “Done”/);
  });

  /* ── Assignees: people and personas ── */

  describe('assignees', () => {
    const PERSON_TICKET = {
      assigned_to: null,
      assigned_user_id: 'u2',
      assigned_user: { id: 'u2', display_name: 'Sam Rivera', email: 'sam@example.test' },
    };

    it('shows a person assignee with an avatar in the list, a persona as before', () => {
      hooks.useTickets.mockReturnValue({
        data: [ticket(PERSON_TICKET), ticket({ id: 'tk_2', title: 'Second' })],
        isLoading: false,
        error: null,
      });
      const { container } = render(<TicketsTab projectId="proj_1" />);
      const person = container.querySelector('[data-assignee="person"]')!;
      expect(person).toHaveTextContent('SSam Rivera');
      expect(person.querySelector('[aria-hidden="true"]')).toHaveTextContent('S');
      expect(container.querySelector('[data-assignee="persona"]')).toHaveTextContent('atlas');
    });

    it('shows a person assignee on board cards', () => {
      localStorage.setItem('sfs-tickets-view', 'board');
      hooks.useTickets.mockReturnValue({ data: [ticket(PERSON_TICKET)], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" />);
      const card = screen.getByRole('button', { name: /Add session export/ });
      expect(within(card).getByText('Sam Rivera')).toBeInTheDocument();
    });

    it('shows a person assignee in the detail header', () => {
      hooks.useTickets.mockReturnValue({ data: [ticket(PERSON_TICKET)], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      const header = screen.getByRole('button', { name: 'Reassign' }).parentElement!;
      expect(header).toHaveTextContent(/^Assigned to SSam Rivera/);
      expect(header.querySelector('[data-assignee="person"]')).toBeInTheDocument();
    });

    it('filters to tickets assigned to me and says so when there are none', async () => {
      const user = userEvent.setup();
      render(<TicketsTab projectId="proj_1" />);
      const toggle = screen.getByRole('button', { name: 'Assigned to me' });
      expect(toggle).toHaveAttribute('aria-pressed', 'false');
      hooks.useTickets.mockReturnValue({ data: [], isLoading: false, error: null });
      await user.click(toggle);
      expect(hooks.useTickets).toHaveBeenLastCalledWith('proj_1', expect.objectContaining({ assigned_user: 'me' }));
      expect(screen.getByRole('button', { name: 'Assigned to me' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByText('No tickets assigned to you.')).toBeInTheDocument();
    });

    it('follows a controlled filter value', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(<TicketsTab projectId="proj_1" assignedToMe onAssignedToMeChange={onChange} />);
      expect(hooks.useTickets).toHaveBeenLastCalledWith('proj_1', expect.objectContaining({ assigned_user: 'me' }));
      await user.click(screen.getByRole('button', { name: 'Assigned to me' }));
      expect(onChange).toHaveBeenCalledWith(false);
    });

    it('creates a ticket assigned to a person, with the persona field cleared', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description (markdown)'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Sam Rivera/ }));
      expect(screen.getByTestId('assignee-note')).toHaveTextContent('Notifies Sam Rivera');
      await user.click(screen.getByRole('button', { name: 'Create' }));
      expect(create.mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ assigned_user_id: 'u2', assigned_to: null }),
      );
    });

    it('creates a ticket assigned to a persona and warns that no one is notified', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description (markdown)'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /atlas/ }));
      expect(screen.getByTestId('assignee-note')).toHaveTextContent(/No one is notified: atlas/);
      await user.click(screen.getByRole('button', { name: 'Create' }));
      expect(create.mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ assigned_to: 'atlas', assigned_user_id: null }),
      );
    });

    it('creates an unassigned ticket by default', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description (markdown)'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Create' }));
      expect(create.mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ assigned_to: null, assigned_user_id: null }),
      );
    });

    it('shows an ineligible person as a field error on create', async () => {
      const user = userEvent.setup();
      const create = makeMutation({
        mutateAsync: vi.fn().mockRejectedValue(
          new ApiError(422, JSON.stringify({ error: { code: 'assignee_not_eligible', message: 'not a member' } })),
        ),
      });
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description (markdown)'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /kim@example\.test/ }));
      await user.click(screen.getByRole('button', { name: 'Create' }));
      expect(await screen.findByRole('alert')).toHaveTextContent("That person can't be assigned tickets in this project.");
      expect(mockAddToast).toHaveBeenCalledWith('error', expect.stringMatching(/can't be assigned/));
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('reassigns from persona to person, sending the persona as null', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Reassign' }));
      const picker = screen.getByRole('button', { name: 'Assignee' });
      expect(picker).toHaveFocus();
      expect(picker).toHaveTextContent('atlas');
      expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
      await user.click(picker);
      await user.click(screen.getByRole('option', { name: /Ola Dev/ }));
      expect(screen.getByTestId('assignee-note')).toHaveTextContent('Notifies you');
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_user_id: 'u1', assigned_to: null },
      });
      expect(mockAddToast).toHaveBeenCalledWith('success', 'Assignee updated');
      expect(screen.queryByRole('group', { name: 'Reassign ticket' })).toBeNull();
      expect(screen.getByRole('button', { name: 'Reassign' })).toHaveFocus();
    });

    it('reassigns from person to persona and maps a conflict error', async () => {
      const user = userEvent.setup();
      const update = makeMutation({
        mutateAsync: vi.fn().mockRejectedValue(
          new ApiError(422, JSON.stringify({ error: { code: '422', message: 'conflict', details: { error: 'assignee_conflict' } } })),
        ),
      });
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket(PERSON_TICKET)], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Reassign' }));
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /sentinel/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: 'sentinel', assigned_user_id: null },
      });
      expect(await screen.findByRole('alert')).toHaveTextContent('A ticket can be assigned to a person or a persona, not both.');
      // The editor stays open so the choice can be fixed.
      expect(screen.getByRole('group', { name: 'Reassign ticket' })).toBeInTheDocument();
    });

    it('unassigns and offers Assign on an unassigned ticket', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Reassign' }));
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Unassigned/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: null, assigned_user_id: null },
      });
      cleanup();
      hooks.useTickets.mockReturnValue({ data: [ticket({ assigned_to: null })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByRole('button', { name: 'Assign' })).toBeInTheDocument();
    });

    it('hides Reassign from an editor who is neither the creator nor a project admin', () => {
      render(
        <TicketsTab
          projectId="proj_1"
          viewer={{ canEdit: true, userId: 'u9' }}
          jira={{ orgId: 'org_1', isProjectAdmin: false, isEnterprise: false }}
        />,
      );
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.queryByRole('button', { name: 'Reassign' })).toBeNull();
    });

    it('offers Reassign to the ticket creator even without edit rights', () => {
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByRole('button', { name: 'Reassign' })).toBeInTheDocument();
    });
  });
});
