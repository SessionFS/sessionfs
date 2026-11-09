import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TicketsTab from './TicketsTab';
import { ApiError } from '../api/client';
import { axe } from '../test/axe';

const { hooks, mockAddToast, mockUseMe, mockUseJiraLink, mockUsePersonas, mockClient, mockInvalidate } = vi.hoisted(() => ({
  mockClient: {
    approveTicket: vi.fn(),
    dismissTicket: vi.fn(),
    watchTicket: vi.fn(),
    updateTicket: vi.fn(),
  },
  mockInvalidate: vi.fn(),
  mockUseJiraLink: vi.fn(),
  mockUsePersonas: vi.fn(),
  hooks: {
    useTickets: vi.fn(),
    useTicket: vi.fn(),
    useTicketChildren: vi.fn(),
    useTicketComments: vi.fn(),
    useTicketJiraSyncEvents: vi.fn(),
    useTicketEvents: vi.fn(),
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

vi.mock('../reminders/RemindersStrip', () => ({ RemindersStrip: () => null }));
vi.mock('../hooks/useTickets', () => hooks);
vi.mock('../hooks/useJiraTickets', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useJiraTickets')>()),
  useJiraLink: mockUseJiraLink,
}));
// External contacts in the assignee picker (GitHub #125): none here.
vi.mock('../hooks/useContacts', () => ({
  useActiveContacts: () => ({ data: { items: [], next_before: null }, isLoading: false, isError: false }),
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
// The bulk bar calls the API client directly (one call per ticket).
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ auth: { client: mockClient } }) }));
vi.mock('@tanstack/react-query', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-query')>()),
  useQueryClient: () => ({ invalidateQueries: mockInvalidate }),
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

/** A project admin who did not create the fixture ticket (u1 did). */
const ADMIN = { canEdit: true, userId: 'u9', isProjectAdmin: true };

function eventsResult(events: unknown[], extra: Record<string, unknown> = {}) {
  return {
    data: { pages: [{ events, next_before: null }], pageParams: [''] },
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
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
  hooks.useTicketEvents.mockReturnValue(eventsResult([]));
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

const defaultMatchMedia = window.matchMedia;
afterEach(() => {
  vi.clearAllMocks();
  window.matchMedia = defaultMatchMedia;
});

/** The pane opens on Overview; the timeline and the composer are on Activity. */
function openActivity() {
  fireEvent.click(screen.getByRole('tab', { name: /^Activity/ }));
}
/** The assignee property in the pane header: "Reassign, assigned to <name>". */
const REASSIGN = /^Reassign,/;
const ASSIGN = /^Assign,/;

/** Make the split-pane media query (>= 1360 px) match. */
function wideViewport() {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === '(min-width: 1360px)',
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe('TicketsTab', () => {
  it('renders one-line rows with a status button that names the status', () => {
    render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByRole('button', { name: 'Add session export' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Status: Suggested. Show status actions' })).toBeInTheDocument();
    // The status group header carries the same words.
    expect(screen.getByRole('button', { name: /^Suggested/ })).toHaveAttribute('aria-expanded', 'true');
  });

  it('shows review, blocker, progress and Jira signals on rows', () => {
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({ id: 'tk_r1', title: 'Changes asked', status: 'review', review_summary: { last_round: 1, last_verdict: 'changes', open_findings: 2, trusted: true } }),
        ticket({ id: 'tk_r2', title: 'Clean one', status: 'review', jira_key: 'WID-43', review_summary: { last_round: 2, last_verdict: 'clean', open_findings: 0, trusted: true } }),
        ticket({ id: 'tk_r3', title: 'Not reviewed yet', status: 'review' }),
        ticket({ id: 'tk_b', title: 'Waiting one', status: 'open', open_dependency_count: 1 }),
        ticket({ id: 'tk_i', title: 'Roll-up', kind: 'issue', status: 'in_progress', child_counts: { total: 4, finished: 1 } }),
      ],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    const row = (title: string) => screen.getByRole('button', { name: title }).closest('.tk-row') as HTMLElement;
    expect(within(row('Changes asked').querySelector('.tk-sig') as HTMLElement).getByText('R1 · 2 open')).toBeInTheDocument();
    expect(within(row('Clean one').querySelector('.tk-sig') as HTMLElement).getByText('R2 clean')).toBeInTheDocument();
    expect(within(row('Clean one')).getByText('WID-43')).toBeInTheDocument();
    expect(within(row('Not reviewed yet').querySelector('.tk-sig') as HTMLElement).getByText('Awaiting review')).toBeInTheDocument();
    expect(within(row('Waiting one').querySelector('.tk-sig') as HTMLElement).getByText('Waiting on 1')).toBeInTheDocument();
    expect(row('Roll-up')).toHaveTextContent('1/4 child tasks finished');
    expect(screen.queryByText(/\b0 criteria\b/)).not.toBeInTheDocument();
  });

  it('switches saved views and fetches the whole list once', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket(), ticket({ id: 'tk_2', title: 'Open one', status: 'open' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByRole('tab', { name: /Triage.*1 ticket/ })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /Triage/ }));
    expect(screen.getByRole('tab', { name: /Triage/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Add session export')).toBeInTheDocument();
    expect(screen.queryByText('Open one')).toBeNull();
    expect(hooks.useTickets).toHaveBeenLastCalledWith('proj_1');
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
    const title = screen.getByRole('button', { name: 'Add session export' });
    const statusBtn = screen.getByRole('button', { name: /^Status: Suggested/ });
    // Siblings: neither control sits inside the other.
    expect(title.contains(statusBtn)).toBe(false);
    expect(statusBtn.contains(title)).toBe(false);
    await user.click(statusBtn);
    expect(screen.getByRole('menuitem', { name: /Approve/i })).toBeInTheDocument();
    expect(screen.getByText(/happen where the work runs/)).toBeInTheDocument();
    expect(screen.queryByText('Implement export endpoint.')).toBeNull();
    expect(title).toHaveAttribute('aria-expanded', 'false');
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
    const pane = screen.getByRole('dialog');
    expect(within(pane).queryByRole('button', { name: /Approve/i })).toBeNull();
    // Dismissing an approved ticket is in the status menu, not the next step.
    fireEvent.click(within(pane).getByRole('button', { name: /Show status actions/ }));
    expect(within(pane).getByRole('menuitem', { name: /Dismiss/ })).toBeInTheDocument();
    expect(within(pane).queryByRole('menuitem', { name: 'Approve' })).toBeNull();
  });

  it('opens the new ticket modal', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New ticket/i }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /New ticket/i })).toBeInTheDocument();
  });

  it('shows the acceptance-criteria example on separate lines, not a literal backslash-n', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New ticket/i }));
    const placeholder = screen.getByTitle('Acceptance criteria (one per line)').getAttribute('placeholder') ?? '';
    expect(placeholder.split('\n')).toHaveLength(3);
    expect(placeholder).not.toContain('\\n');
  });

  it('filters to Issues with the quick filter chip', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket({ status: 'open' }), ticket({ id: 'tk_i', title: 'An issue', kind: 'issue', status: 'open' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    const chip = screen.getByRole('button', { name: 'Issues only' });
    await user.click(chip);
    expect(chip).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('An issue')).toBeInTheDocument();
    expect(screen.queryByText('Add session export')).toBeNull();
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

  it('explains an empty project and an empty view', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByText('No tickets in this project yet')).toBeInTheDocument();
    expect(screen.getByText('sfs ticket create')).toBeInTheDocument();
    cleanup();
    hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
    render(<TicketsTab projectId="proj_1" />);
    await user.click(screen.getByRole('tab', { name: /Blocked/ }));
    expect(screen.getByText('Nothing blocked')).toBeInTheDocument();
    await user.type(screen.getByRole('searchbox', { name: 'Search tickets' }), 'zzz');
    expect(screen.getByText('No tickets match “zzz”')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByRole('searchbox', { name: 'Search tickets' })).toHaveValue('');
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
    const tab = screen.getByRole('tab', { name: /^Child tasks/ });
    expect(tab).toHaveTextContent('Child tasks0/2');
    fireEvent.click(tab);
    expect(screen.getByRole('button', { name: /Sentinel header audit/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Dashboard kind filter/ })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '1 in review, 1 in progress' })).toBeInTheDocument();
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
    const breadcrumb = screen.getByRole('button', { name: /^Parent Issue:/ });
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

  it('exposes a kind switch and a Part of Issue picker for Tasks only in the new ticket dialog', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByRole('button', { name: /New ticket/i }));
    const kind = screen.getByRole('group', { name: 'Kind' });
    expect(within(kind).getByRole('button', { name: 'Task' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /Part of Issue/ })).toBeInTheDocument();
    await user.click(within(kind).getByRole('button', { name: 'Issue' }));
    expect(within(kind).getByRole('button', { name: 'Issue' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('button', { name: /Part of Issue/ })).toBeNull();
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

  it('board cards show the waiting-on signal only when a ticket has open blockers', () => {
    localStorage.setItem('sfs-tickets-view', 'board');
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({ id: 'tk_none', title: 'No counts', status: 'open', acceptance_criteria: [], depends_on: [] }),
        ticket({ id: 'tk_two', title: 'Two of each', status: 'open', acceptance_criteria: ['a', 'b'], depends_on: ['tk_x', 'tk_y'] }),
        ticket({ id: 'tk_x', title: 'Blocker one', status: 'open' }),
        ticket({ id: 'tk_y', title: 'Blocker two', status: 'in_progress' }),
      ],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    const card = (title: string) => screen.getByText(title).closest('button')!.textContent ?? '';
    expect(card('No counts')).not.toMatch(/unfinished|·/);
    expect(card('Two of each')).toMatch(/2 unfinished dependencies/);
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
    openActivity();
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

  it('opens the list detail as a slide-over below 1360px', () => {
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    const dialog = screen.getByRole('dialog', { name: 'Add session export' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText('Implement export endpoint.')).toBeInTheDocument();
  });

  it('opens the list detail as a split pane beside the list at 1360px and wider', () => {
    wideViewport();
    render(<TicketsTab projectId="proj_1" />);
    const row = screen.getByText('Add session export').closest('button')!;
    row.focus();
    fireEvent.click(row);
    expect(screen.queryByRole('dialog')).toBeNull();
    const pane = screen.getByRole('region', { name: 'Add session export' });
    expect(within(pane).getByText('Implement export endpoint.')).toBeInTheDocument();
    // The title takes focus so the pane is announced.
    expect(within(pane).getByRole('heading', { name: 'Add session export' })).toHaveFocus();
    // The list stays on screen beside it.
    expect(row).toBeInTheDocument();
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
    openActivity();

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
    openActivity();

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
    openActivity();
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
    openActivity();

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
    openActivity();
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
    openActivity();

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
    const pane = screen.getByRole('dialog');
    expect(within(pane).getByRole('heading', { name: 'Add session export' })).toBeInTheDocument();
    expect(within(pane).getByRole('button', { name: /^Status: Suggested/ })).toBeInTheDocument();
    expect(within(pane).getByRole('button', { name: REASSIGN })).toHaveTextContent('atlas');
    const next = within(pane).getByRole('region', { name: 'Next step' });
    expect(next).toHaveTextContent('Needs approval');
    expect(within(next).getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('lists dependencies as rows and shows completion notes', () => {
    hooks.useTickets.mockReturnValue({
      data: [ticket({ depends_on: ['tk_dep'], completion_notes: 'Shipped behind a flag.' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" />);
    fireEvent.click(screen.getByText('Add session export'));
    const deps = screen.getByText('Depends on').closest('section')!;
    expect(within(deps).getByRole('button', { name: /tk_dep/ })).toBeInTheDocument();
    expect(screen.getByText('Shipped behind a flag.')).toBeInTheDocument();
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
    openActivity();

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
    openActivity();

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
    openActivity();
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

    it('shows a person assignee with an avatar in the list, a persona by name', () => {
      hooks.useTickets.mockReturnValue({
        data: [ticket(PERSON_TICKET), ticket({ id: 'tk_2', title: 'Second' })],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      const person = screen.getByRole('button', { name: 'Assignee: Sam Rivera (person). Reassign' });
      expect(person.querySelector('[aria-hidden="true"]')).toHaveTextContent('S');
      expect(screen.getByRole('button', { name: 'Assignee: atlas (agent). Reassign' })).toBeInTheDocument();
    });

    it('shows a person assignee on board cards', () => {
      localStorage.setItem('sfs-tickets-view', 'board');
      hooks.useTickets.mockReturnValue({ data: [ticket(PERSON_TICKET)], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      const card = screen.getByRole('button', { name: /Add session export/ });
      expect(card).toHaveTextContent('Assigned to Sam Rivera');
    });

    it('shows a person assignee in the detail header', () => {
      hooks.useTickets.mockReturnValue({ data: [ticket(PERSON_TICKET)], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      const prop = screen.getByRole('button', { name: REASSIGN });
      expect(prop).toHaveTextContent(/Sam Rivera/);
      expect(prop.querySelector('[data-assignee="person"]')).toBeInTheDocument();
    });

    it('shows tickets assigned to me in Mine and says so when there are none', async () => {
      const user = userEvent.setup();
      hooks.useTickets.mockReturnValue({
        data: [ticket({ ...PERSON_TICKET, assigned_user_id: 'u9', assigned_user: { id: 'u9', display_name: 'Me', email: 'me@example.test' } }), ticket({ id: 'tk_2', title: 'Not mine' })],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('tab', { name: /Mine/ }));
      expect(screen.getByText('Add session export')).toBeInTheDocument();
      expect(screen.queryByText('Not mine')).toBeNull();
      expect(screen.getByRole('button', { name: /Assignee: Me \(you\)/ })).toBeInTheDocument();
      cleanup();
      hooks.useTickets.mockReturnValue({ data: [ticket()], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('tab', { name: /Mine/ }));
      expect(screen.getByText('Nothing assigned to you')).toBeInTheDocument();
    });

    it('follows controlled list params', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      const params = { view: 'triage', group: 'none', sort: 'priority', q: '', issuesOnly: false, highOnly: false, showFinished: false } as const;
      render(<TicketsTab projectId="proj_1" params={params} onParamsChange={onChange} />);
      expect(screen.getByRole('tab', { name: /Triage/ })).toHaveAttribute('aria-selected', 'true');
      await user.click(screen.getByRole('tab', { name: /Overdue/ }));
      expect(onChange).toHaveBeenCalledWith({ ...params, view: 'overdue' });
    });

    it('creates a ticket assigned to a person, with the persona field cleared', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Sam Rivera/ }));
      expect(screen.getByTestId('assignee-note')).toHaveTextContent('Notifies Sam Rivera');
      await user.click(screen.getByRole('button', { name: 'Create ticket' }));
      expect(create.mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ assigned_user_id: 'u2', assigned_to: null }),
      );
    });

    it('creates a ticket assigned to a persona and warns that no one is notified', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /atlas/ }));
      expect(screen.getByTestId('assignee-note')).toHaveTextContent(/No one is notified: atlas/);
      await user.click(screen.getByRole('button', { name: 'Create ticket' }));
      expect(create.mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ assigned_to: 'atlas', assigned_user_id: null }),
      );
    });

    it('creates an unassigned ticket by default', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Create ticket' }));
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
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      await user.type(screen.getByLabelText('Title'), 'Export');
      await user.type(screen.getByLabelText('Description'), 'Body');
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /kim@example\.test/ }));
      await user.click(screen.getByRole('button', { name: 'Create ticket' }));
      expect(await screen.findByRole('alert')).toHaveTextContent("That person can't be assigned tickets in this project.");
      expect(mockAddToast).toHaveBeenCalledWith('error', expect.stringMatching(/can't be assigned/));
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('reassigns from persona to person, sending the persona as null', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
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
      expect(screen.getByRole('button', { name: REASSIGN })).toHaveFocus();
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
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /sentinel/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: 'sentinel', assigned_user_id: null },
      });
      expect(await screen.findByRole('alert')).toHaveTextContent('A ticket can be assigned to one person, persona or contact at a time.');
      // The editor stays open so the choice can be fixed.
      expect(screen.getByRole('group', { name: 'Reassign ticket' })).toBeInTheDocument();
    });

    it('unassigns and offers Assign on an unassigned ticket', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Unassigned/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: null, assigned_user_id: null },
      });
      cleanup();
      hooks.useTickets.mockReturnValue({ data: [ticket({ assigned_to: null })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByRole('button', { name: ASSIGN })).toBeInTheDocument();
    });

    it('lets an editor who is neither the creator nor a project admin assign and save', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ assigned_to: null })], isLoading: false, error: null });
      render(
        <TicketsTab
          projectId="proj_1"
          viewer={{ canEdit: true, userId: 'u9', isProjectAdmin: false }}
          jira={{ orgId: 'org_1', isProjectAdmin: false, isEnterprise: false }}
        />,
      );
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: ASSIGN }));
      // Nothing to clear on an unassigned ticket.
      expect(screen.queryByRole('button', { name: 'Unassign' })).toBeNull();
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Sam Rivera/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_user_id: 'u2', assigned_to: null },
      });
      expect(mockAddToast).toHaveBeenCalledWith('success', 'Assignee updated');
      // Field edits keep the creator-or-admin rule.
      expect(screen.queryByRole('button', { name: /due date/ })).toBeNull();
    });

    it('unassigns in one click with explicit nulls', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket(PERSON_TICKET)], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: true, userId: 'u9', isProjectAdmin: false }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
      await user.click(screen.getByRole('button', { name: 'Unassign' }));
      expect(update.mutateAsync).toHaveBeenCalledTimes(1);
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: null, assigned_user_id: null },
      });
      expect(mockAddToast).toHaveBeenCalledWith('success', 'Unassigned');
      expect(screen.queryByRole('group', { name: 'Reassign ticket' })).toBeNull();
    });

    it('asks before a non-editor creator unassigns an approved ticket', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
      await user.click(screen.getByRole('button', { name: 'Unassign' }));
      expect(update.mutateAsync).not.toHaveBeenCalled();
      await user.click(screen.getByRole('button', { name: 'Save and send back for approval' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: null, assigned_user_id: null },
      });
    });

    it('hides Assign from a reader who is neither the creator nor a project admin', () => {
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u9', isProjectAdmin: false }} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.queryByRole('button', { name: REASSIGN })).toBeNull();
      expect(screen.queryByRole('button', { name: ASSIGN })).toBeNull();
    });

    it('offers Reassign to the ticket creator even without edit rights', () => {
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByRole('button', { name: REASSIGN })).toBeInTheDocument();
    });
  });
  /* ── Scheduling, provenance and history (GitHub #113) ── */

  describe('scheduling and history', () => {
    const year = new Date().getFullYear();
    const validation422 = (msg: string) =>
      new ApiError(
        422,
        JSON.stringify({
          error: { code: '422', message: 'Validation error', details: { errors: [{ loc: ['body', 'estimated_effort'], msg: `Value error, ${msg}`, type: 'value_error' }] } },
        }),
      );
    const diff = (...lines: string[]) =>
      ['**Ticket fields updated** via `update_ticket` (tk_835a876529de4551):', ...lines, '\n_Full per-field old/new values preserved in `ticket_edits`._'].join('\n');
    const sysComment = (id: string, content: string, at: string, author = 'u2') => ({
      id, ticket_id: 'tk_1', author_user_id: author, author_persona: 'system', content, session_id: null, created_at: at, source: 'sessionfs',
    });
    const evt = (id: string, action: string, at: string, extra: Record<string, unknown> = {}) => ({
      id, action, field: null, before: null, after: null,
      actor: { type: 'user', user_id: 'u2', display_name: 'Sam Rivera', persona: null, label: null },
      created_at: at, ...extra,
    });

    it('shows the creator with email and date, and who resolved it', () => {
      hooks.useTickets.mockReturnValue({
        data: [
          ticket({
            status: 'done',
            created_at: '2026-05-14T12:00:00Z',
            created_by: { user_id: 'u1', display_name: 'Ola Dev', email: 'o***@example.test', persona: 'atlas', session_id: null },
            resolved_by: { user_id: 'u2', display_name: 'Sam Rivera', email: 'sam@example.test' },
            resolved_at: '2026-05-20T12:00:00Z',
          }),
        ],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByRole('button', { name: /1 finished ticket hidden/ }));
      fireEvent.click(screen.getByText('Add session export'));
      const created = screen.getByText('Created', { selector: 'dt' }).closest('div')!;
      expect(created).toHaveTextContent('Ola Dev (atlas) · o***@example.test · May 14, 2026');
      const resolved = screen.getByText('Resolved', { selector: 'dt' }).closest('div')!;
      expect(resolved).toHaveTextContent('by Sam Rivera · sam@example.test on May 20, 2026');
    });

    it('falls back to the persona, then Unknown, for the creator', () => {
      hooks.useTickets.mockReturnValue({ data: [ticket({ created_by_persona: 'compass' })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByText('Created', { selector: 'dt' }).closest('div')).toHaveTextContent(/^Createdcompass/);
      cleanup();
      hooks.useTickets.mockReturnValue({ data: [ticket()], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByText('Created', { selector: 'dt' }).closest('div')).toHaveTextContent(/^CreatedUnknown/);
    });

    it('marks an open ticket past its due date as overdue, but not a finished one', () => {
      hooks.useTickets.mockReturnValue({
        data: [ticket({ status: 'open', due_date: '2026-01-02', estimated_effort: '2d 4h' })],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      // The row says how late it is.
      expect(screen.getByText(/^\d+d late$/)).toBeInTheDocument();
      fireEvent.click(screen.getByText('Add session export'));
      const due = screen.getByText('Due', { selector: 'dt' }).closest('div')!;
      expect(within(due).getByText(/Jan 2/)).toHaveClass('text-danger');
      expect(within(due).getByText('Overdue')).toBeInTheDocument();
      expect(screen.getByText('Estimate', { selector: 'dt' }).closest('div')).toHaveTextContent('2d 4h');
      cleanup();
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'done', due_date: '2026-01-02' })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByRole('button', { name: /1 finished ticket hidden/ }));
      expect(screen.queryByText(/d late$/)).toBeNull();
      expect(screen.getByText(/^Jan 2/)).toBeInTheDocument();
    });

    it('sets, clears and cancels the due date inline', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ due_date: `${year + 1}-03-04` })], isLoading: false, error: null });
      const { container } = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Edit due date' }));
      const editor = screen.getByRole('group', { name: 'Edit due date' });
      const input = within(editor).getByLabelText('Due date');
      expect(input).toHaveValue(`${year + 1}-03-04`);
      expect(input).toHaveFocus();
      expect(await axe(container)).toHaveNoViolations();
      fireEvent.change(input, { target: { value: `${year + 1}-04-01` } });
      await user.click(within(editor).getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { due_date: `${year + 1}-04-01` } });
      // Closed again, focus back on the trigger.
      expect(screen.queryByRole('group', { name: 'Edit due date' })).toBeNull();
      expect(screen.getByRole('button', { name: 'Edit due date' })).toHaveFocus();

      await user.click(screen.getByRole('button', { name: 'Edit due date' }));
      await user.click(screen.getByRole('button', { name: 'Clear' }));
      expect(update.mutateAsync).toHaveBeenLastCalledWith({ ticketId: 'tk_1', body: { due_date: null } });

      update.mutateAsync.mockClear();
      await user.click(screen.getByRole('button', { name: 'Edit due date' }));
      await user.keyboard('{Escape}');
      expect(screen.queryByRole('group', { name: 'Edit due date' })).toBeNull();
      expect(update.mutateAsync).not.toHaveBeenCalled();
    });

    it('validates the estimate before sending and shows the server 422 message', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      const { container } = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByText('No estimate')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Set estimate' }));
      const field = screen.getByLabelText('Estimate');
      await user.type(field, '2 days{Enter}');
      expect(update.mutateAsync).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toHaveTextContent('Use Jira durations like 2d 4h');
      expect(field).toHaveAttribute('aria-invalid', 'true');
      expect(await axe(container)).toHaveNoViolations();

      update.mutateAsync.mockRejectedValueOnce(validation422("estimated_effort must be a Jira duration like '2d 4h'"));
      await user.clear(field);
      await user.type(field, ' 2d4h {Enter}');
      expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { estimated_effort: '2d4h' } });
      expect(await screen.findByRole('alert')).toHaveTextContent("estimated_effort must be a Jira duration like '2d 4h'");
      // Still editing after a refusal.
      expect(screen.getByRole('group', { name: 'Edit estimate' })).toBeInTheDocument();
    });

    it('offers no scheduling edits to a viewer who may not edit the ticket', () => {
      hooks.useTickets.mockReturnValue({ data: [ticket({ due_date: '2026-12-01' })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u9' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.queryByRole('button', { name: /due date/ })).toBeNull();
      expect(screen.queryByRole('button', { name: /estimate/ })).toBeNull();
    });

    it('merges the event log into the timeline as sentences, without repeating diff comments', () => {
      hooks.useTickets.mockReturnValue({
        data: [ticket({ created_by: { user_id: 'u1', display_name: 'Ola Dev', email: 'ola@example.test' } })],
        isLoading: false,
        error: null,
      });
      hooks.useTicketComments.mockReturnValue({
        data: [
          // Posted by the same edit as the e_prio event, a second later.
          { id: 'c_diff', ticket_id: 'tk_1', author_user_id: 'u2', author_persona: 'system', content: diff('- `priority`: "medium" → "high"'), session_id: null, created_at: '2026-05-14T03:00:01Z', source: 'sessionfs' },
          { id: 'c_old', ticket_id: 'tk_1', author_user_id: 'u1', author_persona: 'system', content: 'Edited before the log existed', session_id: null, created_at: '2026-05-14T00:30:00Z', source: 'sessionfs' },
          { id: 'c_talk', ticket_id: 'tk_1', author_user_id: 'u2', author_persona: null, content: 'On it', session_id: null, created_at: '2026-05-14T04:30:00Z', source: 'sessionfs' },
        ],
      });
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([
          // Newest first, as the API returns them.
          evt('e_jira', 'jira_linked', '2026-05-14T06:00:00Z', { after: { issue_key: 'ACME-12' }, actor: { type: 'service', label: 'ci-bot' } }),
          evt('e_due', 'due_date_set', '2026-05-14T05:00:00Z', { field: 'due_date', after: `${year}-11-15` }),
          evt('e_status', 'status_changed', '2026-05-14T04:00:00Z', { field: 'status', before: 'open', after: 'in_progress', actor: { type: 'jira', label: 'Priya' } }),
          evt('e_prio', 'field_edited', '2026-05-14T03:00:00Z', { field: 'priority', before: 'medium', after: 'high' }),
          evt('e_desc', 'field_edited', '2026-05-14T02:00:00Z', { field: 'description', before: { changed: true, length: 10 }, after: { changed: true, length: 30 } }),
          evt('e_assign', 'assigned', '2026-05-14T01:00:00Z', { field: 'assigned_to', after: { persona: 'reviewer' }, actor: { type: 'system' } }),
          evt('e_created', 'created', '2026-05-14T00:00:00Z'),
        ]),
      );
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      const rows = within(screen.getByRole('region', { name: 'Activity' }))
        .getAllByRole('listitem')
        .map((li) => li.textContent ?? '');
      expect(rows).toHaveLength(9);
      expect(rows[0]).toMatch(/^Created by Ola Dev/);
      expect(rows[1]).toMatch(/Edited before the log existed/);
      expect(rows[2]).toMatch(/^SessionFS assigned this to the reviewer persona/);
      expect(rows[3]).toMatch(/^Sam Rivera edited the description/);
      expect(rows[4]).toMatch(/^Sam Rivera changed the priority from medium to high/);
      expect(rows[5]).toMatch(/^Jira \(Priya\) moved this from open to in progress/);
      expect(rows[6]).toMatch(/On it/);
      expect(rows[7]).toMatch(/^Sam Rivera set the due date to Nov 15/);
      expect(rows[8]).toMatch(/^Service key ci-bot linked this to Jira ACME-12/);
      // The diff comment repeats e_prio, so it is not shown.
      expect(rows.some((r) => /Ticket fields updated/.test(r))).toBe(false);
      expect(rows.join(' ')).not.toMatch(/[{}]/);
    });

    it('drops the resolved stamp when the log already shows the move to done', () => {
      hooks.useTickets.mockReturnValue({
        data: [ticket({ status: 'done', resolved_at: '2026-05-15T00:00:00Z' })],
        isLoading: false,
        error: null,
      });
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_done', 'status_changed', '2026-05-15T00:00:00Z', { field: 'status', before: 'review', after: 'done' })]),
      );
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByRole('button', { name: /1 finished ticket hidden/ }));
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      const rows = within(screen.getByRole('region', { name: 'Activity' })).getAllByRole('listitem').map((li) => li.textContent ?? '');
      expect(rows.some((r) => /^Resolved/.test(r))).toBe(false);
      expect(rows.some((r) => /moved this from in review to done/.test(r))).toBe(true);
    });

    it('loads earlier history from where the loaded events stop', async () => {
      const user = userEvent.setup();
      const fetchNextPage = vi.fn();
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_new', 'approved', '2026-05-14T05:00:00Z')], { hasNextPage: true, fetchNextPage }),
      );
      hooks.useTicketComments.mockReturnValue({
        data: [{ id: 'c1', ticket_id: 'tk_1', author_user_id: 'u1', author_persona: null, content: 'Early note', session_id: null, created_at: '2026-05-14T02:00:00Z', source: 'sessionfs' }],
      });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      const rows = within(screen.getByRole('region', { name: 'Activity' })).getAllByRole('listitem').map((li) => li.textContent ?? '');
      expect(rows[2]).toMatch(/Show earlier history/);
      expect(rows[3]).toMatch(/approved this ticket/);
      await user.click(screen.getByRole('button', { name: 'Show earlier history' }));
      expect(fetchNextPage).toHaveBeenCalled();
    });

    it('names the person an assignment event points at', () => {
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_a', 'assigned', '2026-05-14T05:00:00Z', { field: 'assigned_user_id', after: { user_id: 'u3' } })]),
      );
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      // u3 is only known from the assignable-users list, fetched for it.
      expect(screen.getByText(/assigned this to kim@example.test/)).toBeInTheDocument();
      expect(hooks.useAssignableUsers).toHaveBeenCalledWith('proj_1', true);
    });

    it('shows overdue tickets in the Overdue view', async () => {
      const user = userEvent.setup();
      hooks.useTickets.mockReturnValue({
        data: [ticket({ status: 'open', due_date: '2026-01-01' }), ticket({ id: 'tk_2', title: 'On time', status: 'open', due_date: '2999-01-01' })],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      const tab = screen.getByRole('tab', { name: /Overdue.*1 ticket/ });
      await user.click(tab);
      expect(tab).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByText('Add session export')).toBeInTheDocument();
      expect(screen.queryByText('On time')).toBeNull();
    });

    it('says nothing is overdue in an empty Overdue view', () => {
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
      const params = { view: 'overdue', group: 'status', sort: 'priority', q: '', issuesOnly: false, highOnly: false, showFinished: false } as const;
      render(<TicketsTab projectId="proj_1" params={params} onParamsChange={vi.fn()} />);
      expect(screen.getByText('Nothing overdue')).toBeInTheDocument();
    });

    it('keeps system notes that are not an update diff, and diffs the events cannot show', () => {
      hooks.useTicketComments.mockReturnValue({
        data: [
          sysComment('c_note', 'Changed by a non-member after approval; needs approval again before it can be worked on.', '2026-05-14T03:00:01Z'),
          sysComment('c_desc', diff('- `description`: "Old" → "New text"'), '2026-05-14T04:00:01Z'),
          sysComment('c_unfenced', diff('- `priority`: "low" → "high"', '\n_Unfenced edit_ — no `lease_epoch` passed; last-write-wins.'), '2026-05-14T05:00:01Z'),
          sysComment('c_mixed', diff('- `due_date`: None → "2026-12-01"', '- `depends_on`: [0 item(s)] → [2 item(s)]'), '2026-05-14T06:00:01Z'),
        ],
      });
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([
          evt('e_mixed2', 'field_edited', '2026-05-14T06:00:00Z', { field: 'depends_on', after: { count: 2, items: ['a', 'b'] } }),
          evt('e_mixed1', 'due_date_set', '2026-05-14T06:00:00Z', { field: 'due_date', after: '2026-12-01' }),
          evt('e_prio', 'field_edited', '2026-05-14T05:00:00Z', { field: 'priority', before: 'low', after: 'high' }),
          evt('e_desc', 'field_edited', '2026-05-14T04:00:00Z', { field: 'description', after: { changed: true, length: 8 } }),
          evt('e_back', 'status_changed', '2026-05-14T03:00:00Z', { field: 'status', before: 'open', after: 'suggested' }),
        ]),
      );
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      const text = within(screen.getByRole('region', { name: 'Activity' })).getAllByRole('listitem').map((li) => li.textContent ?? '').join('\n');
      expect(text).toMatch(/needs approval again/);
      expect(text).toMatch(/`description`: "Old" → "New text"/);
      expect(text).toMatch(/Unfenced edit/);
      expect(text).toMatch(/`depends_on`/);
    });

    it('keeps a real comment posted under the "system" persona', () => {
      hooks.useTicketComments.mockReturnValue({
        data: [sysComment('c_fake', 'Heads up: the deploy is paused.', '2026-05-14T03:00:01Z')],
      });
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_prio', 'field_edited', '2026-05-14T03:00:00Z', { field: 'priority', before: 'low', after: 'high' })]),
      );
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      expect(screen.getByText('Heads up: the deploy is paused.')).toBeInTheDocument();
    });

    it('shows a dismissal (or approval) as one row', () => {
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([
          evt('e_dis', 'dismissed', '2026-05-14T06:00:00.200Z'),
          evt('e_cancel', 'status_changed', '2026-05-14T06:00:00.100Z', { field: 'status', before: 'open', after: 'cancelled' }),
          evt('e_app', 'approved', '2026-05-14T05:00:00.200Z'),
          evt('e_open', 'status_changed', '2026-05-14T05:00:00.100Z', { field: 'status', before: 'suggested', after: 'open' }),
        ]),
      );
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      const rows = within(screen.getByRole('region', { name: 'Activity' })).getAllByRole('listitem').map((li) => li.textContent ?? '');
      expect(rows.filter((r) => /dismissed this ticket/.test(r))).toHaveLength(1);
      expect(rows.filter((r) => /approved this ticket/.test(r))).toHaveLength(1);
      expect(rows.some((r) => /moved this from/.test(r))).toBe(false);
    });

    it('offers a retry when earlier history fails to load', async () => {
      const user = userEvent.setup();
      const fetchNextPage = vi.fn();
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_new', 'approved', '2026-05-14T05:00:00Z')], { hasNextPage: true, fetchNextPage, isFetchNextPageError: true }),
      );
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load earlier history.");
      await user.click(screen.getByRole('button', { name: 'Try again' }));
      expect(fetchNextPage).toHaveBeenCalled();
    });

    it('still shows the rest of the timeline when the server has no event log', () => {
      hooks.useTicketEvents.mockReturnValue({
        data: undefined,
        isError: true,
        error: new ApiError(404, JSON.stringify({ error: { code: '404', message: 'Not Found' } })),
        hasNextPage: false,
        isFetchingNextPage: false,
        fetchNextPage: vi.fn(),
      });
      hooks.useTicketComments.mockReturnValue({
        data: [{ id: 'c1', ticket_id: 'tk_1', author_user_id: 'u1', author_persona: 'atlas', content: 'Still here', session_id: null, created_at: '2026-05-14T02:00:00Z', source: 'sessionfs' }],
      });
      render(<TicketsTab projectId="proj_1" />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      const rows = within(screen.getByRole('region', { name: 'Activity' })).getAllByRole('listitem').map((li) => li.textContent ?? '');
      expect(rows[0]).toMatch(/^Created/);
      expect(rows[1]).toMatch(/Still here/);
    });

    it('does not ask a reader for the assignable-users list', () => {
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_a', 'assigned', '2026-05-14T05:00:00Z', { field: 'assigned_user_id', after: { user_id: 'u3' } })]),
      );
      hooks.useAssignableUsers.mockImplementation((_p: string, enabled: boolean) => ({ data: enabled ? ASSIGNABLE : undefined, isLoading: false }));
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u9' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      expect(hooks.useAssignableUsers).toHaveBeenCalledWith('proj_1', false);
      expect(screen.getByText(/assigned this to a teammate/)).toBeInTheDocument();
    });

    it('lets only the creator or a project admin edit the schedule', () => {
      // An editor without admin standing who did not create the ticket.
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: true, userId: 'u9', isProjectAdmin: false }} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.queryByRole('button', { name: /due date/ })).toBeNull();
      cleanup();
      // Admin standing comes from the viewer even without a Jira surface.
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      expect(screen.getByRole('button', { name: 'Set due date' })).toBeInTheDocument();
    });

    it('asks before a non-editor creator edit sends an approved ticket back', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
      const { container } = render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Set estimate' }));
      await user.type(screen.getByLabelText('Estimate'), '3d{Enter}');
      expect(update.mutateAsync).not.toHaveBeenCalled();
      expect(screen.getByText(/Saving this sends the ticket back for approval/)).toBeInTheDocument();
      expect(await axe(container)).toHaveNoViolations();
      await user.click(screen.getByRole('button', { name: 'Keep editing' }));
      expect(screen.queryByText(/sends the ticket back/)).toBeNull();
      await user.click(screen.getByRole('button', { name: 'Save' }));
      await user.click(screen.getByRole('button', { name: 'Save and send back for approval' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { estimated_effort: '3d' } });
    });

    it('saves without asking for an editor, or on a suggested ticket', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Set estimate' }));
      await user.type(screen.getByLabelText('Estimate'), '3d{Enter}');
      expect(update.mutateAsync).toHaveBeenCalled();
    });

    it('keeps the board drawer open when Escape cancels an inline edit', async () => {
      const user = userEvent.setup();
      localStorage.setItem('sfs-tickets-view', 'board');
      try {
        render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
        fireEvent.click(screen.getByText('Add session export'));
        const drawer = screen.getByRole('dialog');
        await user.click(within(drawer).getByRole('button', { name: 'Set due date' }));
        expect(within(drawer).getByLabelText('Due date')).toHaveFocus();
        await user.keyboard('{Escape}');
        expect(screen.getByRole('dialog')).toBeInTheDocument();
        expect(within(drawer).queryByRole('group', { name: 'Edit due date' })).toBeNull();
        // A second Escape, outside any editor, still closes the drawer.
        await user.keyboard('{Escape}');
        expect(screen.queryByRole('dialog')).toBeNull();
      } finally {
        localStorage.removeItem('sfs-tickets-view');
      }
    });

    it('keeps an inline editor open on Escape while its save is in flight', async () => {
      const user = userEvent.setup();
      hooks.useUpdateTicket.mockReturnValue(makeMutation({ isPending: true }));
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Set estimate' }));
      await user.keyboard('{Escape}');
      expect(screen.getByRole('group', { name: 'Edit estimate' })).toBeInTheDocument();
    });

    it('shows a due date 422 under the date field in the new ticket form', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      create.mutateAsync.mockRejectedValueOnce(
        new ApiError(422, JSON.stringify({ error: { code: '422', message: 'Validation error', details: { errors: [{ loc: ['body', 'due_date'], msg: 'Input should be a valid date', type: 'date_from_datetime_parsing' }] } } })),
      );
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      const dialog = screen.getByRole('dialog', { name: 'New ticket' });
      await user.type(within(dialog).getByLabelText('Title'), 'Ship export');
      await user.type(within(dialog).getByLabelText(/Description/), 'Details');
      const due = within(dialog).getByLabelText('Due date');
      fireEvent.change(due, { target: { value: '2027-01-15' } });
      await user.click(within(dialog).getByRole('button', { name: 'Create ticket' }));
      expect(await within(dialog).findByText('Input should be a valid date')).toBeInTheDocument();
      expect(due).toHaveAttribute('aria-invalid', 'true');
    });

    it('keeps an assignment diff when the event names someone the reader cannot resolve', () => {
      const assignDiff = sysComment('c_assign', diff('- `assigned_user_id`: Assigned to Kim Lee'), '2026-05-14T03:00:01Z');
      hooks.useTicketComments.mockReturnValue({ data: [assignDiff] });
      hooks.useTicketEvents.mockReturnValue(
        eventsResult([evt('e_a', 'assigned', '2026-05-14T03:00:00Z', { field: 'assigned_user_id', after: { user_id: 'u3' } })]),
      );
      hooks.useAssignableUsers.mockImplementation((_p: string, enabled: boolean) => ({ data: enabled ? ASSIGNABLE : undefined, isLoading: false }));
      // A reader never loads the assignable users, so u3 stays unnamed.
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u9' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      expect(screen.getByText(/Assigned to Kim Lee/)).toBeInTheDocument();
      cleanup();
      // An editor resolves u3, so the event says it all and the diff folds.
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByText('Add session export'));
      openActivity();
      expect(screen.queryByText(/Assigned to Kim Lee/)).toBeNull();
      expect(screen.getByText(/assigned this to kim@example.test/)).toBeInTheDocument();
    });

    it('moves focus into the approval confirm and keeps the drawer open on Escape there', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
      localStorage.setItem('sfs-tickets-view', 'board');
      try {
        render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
        fireEvent.click(screen.getByText('Add session export'));
        const drawer = screen.getByRole('dialog');
        await user.click(within(drawer).getByRole('button', { name: 'Set estimate' }));
        await user.type(within(drawer).getByLabelText('Estimate'), '3d{Enter}');
        expect(within(drawer).getByRole('button', { name: 'Save and send back for approval' })).toHaveFocus();
        // Keep editing hands focus back to the field.
        await user.click(within(drawer).getByRole('button', { name: 'Keep editing' }));
        expect(within(drawer).getByLabelText('Estimate')).toHaveFocus();
        await user.keyboard('{Enter}');
        expect(within(drawer).getByRole('button', { name: 'Save and send back for approval' })).toHaveFocus();
        await user.keyboard('{Escape}');
        expect(screen.getByRole('dialog')).toBeInTheDocument();
        expect(within(drawer).queryByText(/sends the ticket back/)).toBeNull();
        expect(update.mutateAsync).not.toHaveBeenCalled();
      } finally {
        localStorage.removeItem('sfs-tickets-view');
      }
    });

    it('does not let Enter in the field override a pending Clear', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open', due_date: '2027-02-03' })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: 'Edit due date' }));
      await user.click(screen.getByRole('button', { name: 'Clear' }));
      const field = screen.getByLabelText('Due date');
      fireEvent.submit(field.closest('form')!);
      expect(update.mutateAsync).not.toHaveBeenCalled();
      await user.click(screen.getByRole('button', { name: 'Save and send back for approval' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { due_date: null } });
    });

    it('asks before a non-editor reassignment sends an approved ticket back', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Unassigned/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).not.toHaveBeenCalled();
      expect(screen.getByText(/Saving this sends the ticket back for approval/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Save and send back for approval' })).toHaveFocus();
      await user.click(screen.getByRole('button', { name: 'Keep editing' }));
      expect(screen.getByRole('button', { name: 'Assignee' })).toHaveFocus();
      await user.click(screen.getByRole('button', { name: 'Save' }));
      await user.click(screen.getByRole('button', { name: 'Save and send back for approval' }));
      expect(update.mutateAsync).toHaveBeenCalledWith({
        ticketId: 'tk_1',
        body: { assigned_to: null, assigned_user_id: null },
      });
    });

    it('reassigns without asking on a suggested ticket', async () => {
      const user = userEvent.setup();
      const update = makeMutation();
      hooks.useUpdateTicket.mockReturnValue(update);
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
      fireEvent.click(screen.getByText('Add session export'));
      await user.click(screen.getByRole('button', { name: REASSIGN }));
      await user.click(screen.getByRole('button', { name: 'Assignee' }));
      await user.click(screen.getByRole('option', { name: /Unassigned/ }));
      await user.click(screen.getByRole('button', { name: 'Save' }));
      expect(update.mutateAsync).toHaveBeenCalled();
    });

    it('sends an optional due date and estimate from the new ticket form', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      const { baseElement } = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      const dialog = screen.getByRole('dialog', { name: 'New ticket' });
      await user.type(within(dialog).getByLabelText('Title'), 'Ship export');
      await user.type(within(dialog).getByLabelText(/Description/), 'Details');
      fireEvent.change(within(dialog).getByLabelText('Due date'), { target: { value: `${year + 1}-01-15` } });
      const estimate = within(dialog).getByLabelText('Estimate');
      await user.type(estimate, 'soon');
      await user.click(within(dialog).getByRole('button', { name: 'Create ticket' }));
      expect(create.mutateAsync).not.toHaveBeenCalled();
      expect(within(dialog).getByRole('alert')).toHaveTextContent('Use Jira durations');
      expect(await axe(baseElement)).toHaveNoViolations();

      await user.clear(estimate);
      await user.type(estimate, '1w 2d');
      await user.click(within(dialog).getByRole('button', { name: 'Create ticket' }));
      expect(create.mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'Ship export', due_date: `${year + 1}-01-15`, estimated_effort: '1w 2d' }),
      );
    });

    it('leaves both scheduling fields out of a new ticket when blank', async () => {
      const user = userEvent.setup();
      const create = makeMutation();
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      await user.click(screen.getByRole('button', { name: /New ticket/i }));
      const dialog = screen.getByRole('dialog', { name: 'New ticket' });
      await user.type(within(dialog).getByLabelText('Title'), 'Ship export');
      await user.type(within(dialog).getByLabelText(/Description/), 'Details');
      await user.click(within(dialog).getByRole('button', { name: 'Create ticket' }));
      const body = create.mutateAsync.mock.calls[0][0];
      expect(body).not.toHaveProperty('due_date');
      expect(body).not.toHaveProperty('estimated_effort');
    });
  });

  /* ── Phase 3: keyboard, selection, bulk actions, new-ticket dialog ── */

  describe('keyboard, selection and bulk actions', () => {
    const LIST = [
      ticket({ id: 'tk_a', title: 'Alpha suggested', status: 'suggested', priority: 'high' }),
      ticket({ id: 'tk_b', title: 'Bravo open', status: 'open' }),
      ticket({ id: 'tk_c', title: 'Charlie running', status: 'in_progress' }),
      ticket({ id: 'tk_d', title: 'Delta suggested', status: 'suggested' }),
    ];
    // Grouped by status: In progress, Open, then Suggested (high first).
    const ORDER = ['Charlie running', 'Bravo open', 'Alpha suggested', 'Delta suggested'];

    beforeEach(() => {
      localStorage.removeItem('sfs-tickets-view');
      hooks.useTickets.mockReturnValue({ data: LIST, isLoading: false, error: null });
      mockClient.approveTicket.mockResolvedValue({});
      mockClient.dismissTicket.mockResolvedValue({});
      mockClient.watchTicket.mockResolvedValue({});
      mockClient.updateTicket.mockResolvedValue({});
    });

    const press = (key: string, target: Element = document.activeElement ?? document.body, extra: Record<string, unknown> = {}) =>
      fireEvent.keyDown(target, { key, ...extra });
    const rowTitle = (name: string) => screen.getByRole('button', { name });
    const box = (name: string) => screen.getByRole('checkbox', { name: `Select ${name}` });
    const bar = () => screen.queryByRole('group', { name: 'Bulk actions' });

    it('moves focus through the rows with j / k and arrows, keeping one row in the tab order', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      const titles = ORDER.map(rowTitle);
      expect(titles.map((t) => t.tabIndex)).toEqual([0, -1, -1, -1]);
      press('j', document.body);
      expect(titles[0]).toHaveFocus();
      press('j');
      expect(titles[1]).toHaveFocus();
      expect(titles.map((t) => t.tabIndex)).toEqual([-1, 0, -1, -1]);
      // The other controls on a row follow the same tab stop.
      expect(box('Bravo open').tabIndex).toBe(0);
      expect(box('Charlie running').tabIndex).toBe(-1);
      press('k');
      expect(titles[0]).toHaveFocus();
      press('ArrowDown');
      press('ArrowDown');
      expect(titles[2]).toHaveFocus();
      press('ArrowUp');
      expect(titles[1]).toHaveFocus();
    });

    it('does not fire while typing in a field or while a dialog is open', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      const search = screen.getByRole('searchbox', { name: 'Search tickets' });
      search.focus();
      press('j', search);
      press('c', search);
      press('x', search);
      expect(search).toHaveFocus();
      expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull();
      expect(bar()).toBeNull();
      search.blur();

      press('?', document.body);
      expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
      press('c', document.body);
      press('j', document.body);
      expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull();
      expect(rowTitle('Charlie running')).not.toHaveFocus();
    });

    it('ignores modifier chords such as ⌘K', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      press('c', document.body, { metaKey: true });
      press('j', document.body, { ctrlKey: true });
      expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull();
      expect(rowTitle('Charlie running')).not.toHaveFocus();
    });

    it('focuses search with /, switches views with 1–6 and the layout with b, and opens New ticket with c', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      press('/', document.body);
      expect(screen.getByRole('searchbox', { name: 'Search tickets' })).toHaveFocus();
      (document.activeElement as HTMLElement).blur();

      press('2', document.body);
      expect(screen.getByRole('tab', { name: /Triage/ })).toHaveAttribute('aria-selected', 'true');
      press('6', document.body);
      expect(screen.getByRole('tab', { name: /^All/ })).toHaveAttribute('aria-selected', 'true');

      press('b', document.body);
      expect(screen.getByRole('button', { name: 'Board' })).toHaveAttribute('aria-pressed', 'true');
      press('b', document.body);
      expect(screen.getByRole('button', { name: 'List' })).toHaveAttribute('aria-pressed', 'true');

      press('c', document.body);
      expect(screen.getByRole('dialog', { name: 'New ticket' })).toBeInTheDocument();
    });

    it('opens the focused ticket with o and Enter', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      press('j', document.body);
      press('o');
      expect(screen.getByRole('dialog', { name: 'Charlie running' })).toBeInTheDocument();
    });

    it('leaves Enter on other buttons alone', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      press('j', document.body);
      const chip = screen.getByRole('button', { name: 'Issues only' });
      chip.focus();
      press('Enter', chip);
      expect(screen.queryByRole('dialog', { name: 'Charlie running' })).toBeNull();
    });

    it('opens the status menu of the focused row with s and the assignee editor with a', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      press('j', document.body);
      press('j');
      press('j');
      expect(rowTitle('Alpha suggested')).toHaveFocus();
      press('s');
      expect(screen.getByRole('menu', { name: 'Status actions for tk_a' })).toBeInTheDocument();
      // Keys stay off while the menu is open.
      press('c', document.activeElement!);
      expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull();
      press('Escape', document.activeElement!);
      expect(screen.queryByRole('menu')).toBeNull();

      rowTitle('Alpha suggested').focus();
      press('a');
      expect(screen.getByRole('dialog', { name: 'Reassign tk_a' })).toBeInTheDocument();
    });

    it('toggles selection with x and clears it with Escape', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      press('j', document.body);
      press('x');
      expect(box('Charlie running')).toBeChecked();
      expect(bar()).toHaveTextContent('1 selected');
      press('j');
      press('x');
      expect(bar()).toHaveTextContent('2 selected');
      press('Escape');
      expect(bar()).toBeNull();
      expect(box('Charlie running')).not.toBeChecked();
    });

    it('selects a range with shift-click on a checkbox or a title', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Charlie running'));
      fireEvent.click(box('Alpha suggested'), { shiftKey: true });
      expect(box('Charlie running')).toBeChecked();
      expect(box('Bravo open')).toBeChecked();
      expect(box('Alpha suggested')).toBeChecked();
      expect(box('Delta suggested')).not.toBeChecked();
      expect(bar()).toHaveTextContent('3 selected');
      // Shift-click on a title selects instead of opening the ticket.
      fireEvent.click(rowTitle('Delta suggested'), { shiftKey: true });
      expect(box('Delta suggested')).toBeChecked();
      expect(screen.queryByRole('dialog', { name: 'Delta suggested' })).toBeNull();
    });

    it('explains bulk actions that reach none of the selection and counts partial ones', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Charlie running'));
      const approve = within(bar()!).getByRole('button', { name: 'Approve' });
      expect(approve).toHaveAttribute('aria-disabled', 'true');
      expect(approve).toHaveAccessibleDescription('Only Suggested tickets can be approved');
      const dismiss = within(bar()!).getByRole('button', { name: 'Dismiss' });
      expect(dismiss).toHaveAccessibleDescription('Only Suggested or Open tickets can be dismissed');

      fireEvent.click(box('Alpha suggested'));
      expect(within(bar()!).getByRole('button', { name: 'Approve 1 suggested' })).not.toHaveAttribute('aria-disabled');
      expect(within(bar()!).getByRole('button', { name: 'Dismiss 1 of 2' })).toBeInTheDocument();
    });

    it('never offers Approve to a viewer who may not approve', () => {
      render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u5' }} />);
      fireEvent.click(box('Alpha suggested'));
      const approve = within(bar()!).getByRole('button', { name: 'Approve' });
      expect(approve).toHaveAccessibleDescription('Only project editors can approve tickets');
      fireEvent.click(approve);
      expect(mockClient.approveTicket).not.toHaveBeenCalled();
    });

    it('approves each ticket, reports failures in one line and keeps the failed ones selected', async () => {
      mockClient.approveTicket.mockImplementation((_p: string, id: string) =>
        id === 'tk_d' ? Promise.reject(new Error('Ticket changed')) : Promise.resolve({}),
      );
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Alpha suggested'));
      fireEvent.click(box('Delta suggested'));
      fireEvent.click(within(bar()!).getByRole('button', { name: 'Approve' }));
      await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith('error', 'Approved 1 · 1 failed: Ticket changed'));
      expect(mockClient.approveTicket).toHaveBeenCalledWith('proj_1', 'tk_a');
      expect(mockClient.approveTicket).toHaveBeenCalledWith('proj_1', 'tk_d');
      expect(mockInvalidate).toHaveBeenCalledWith({ queryKey: ['tickets', 'proj_1'] });
      expect(box('Delta suggested')).toBeChecked();
      expect(box('Alpha suggested')).not.toBeChecked();
    });

    it('asks before dismissing in bulk and only dismisses Suggested or Open tickets', async () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Bravo open'));
      fireEvent.click(box('Charlie running'));
      fireEvent.click(within(bar()!).getByRole('button', { name: 'Dismiss 1 of 2' }));
      const confirm = screen.getByRole('dialog', { name: 'Dismiss 1 ticket?' });
      expect(mockClient.dismissTicket).not.toHaveBeenCalled();
      fireEvent.click(within(confirm).getByRole('button', { name: 'Dismiss 1 ticket' }));
      await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith('success', 'Dismissed 1 ticket'));
      expect(mockClient.dismissTicket).toHaveBeenCalledTimes(1);
      expect(mockClient.dismissTicket).toHaveBeenCalledWith('proj_1', 'tk_b');
      expect(bar()).toBeNull();
    });

    it('follows every selected ticket', async () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Bravo open'));
      fireEvent.click(box('Charlie running'));
      fireEvent.click(within(bar()!).getByRole('button', { name: 'Follow' }));
      await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith('success', 'Following 2 tickets'));
      expect(mockClient.watchTicket).toHaveBeenCalledTimes(2);
    });

    it('opens the bulk assign panel with a while rows are selected', () => {
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Bravo open'));
      press('a', document.body);
      expect(screen.getByRole('dialog', { name: 'Assign 1 ticket' })).toBeInTheDocument();
    });

    it('has no axe violations with the bulk bar and the shortcuts dialog', async () => {
      const { container } = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(box('Bravo open'));
      expect(await axe(container)).toHaveNoViolations();
      press('?', document.body);
      expect(await axe(container)).toHaveNoViolations();
    });
  });

  describe('board', () => {
    it('shows six columns, Done holding closed Issues, and Cancelled only as a note', () => {
      localStorage.setItem('sfs-tickets-view', 'board');
      hooks.useTickets.mockReturnValue({
        data: [
          ticket({ id: 'tk_s', title: 'Suggested one', status: 'suggested' }),
          ticket({ id: 'tk_r', title: 'In review one', status: 'review' }),
          ticket({ id: 'tk_closed', title: 'Closed issue', status: 'closed', kind: 'issue' }),
          ticket({ id: 'tk_done', title: 'Done task', status: 'done' }),
          ticket({ id: 'tk_x', title: 'Cancelled one', status: 'cancelled' }),
        ],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" params={{ view: 'all', group: 'status', sort: 'priority', q: '', issuesOnly: false, highOnly: false, showFinished: true }} />);
      const heads = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
      expect(heads.slice(0, 6)).toEqual([
        'Triage1 ticket',
        'Open0 tickets',
        'In progress0 tickets',
        'Blocked0 tickets',
        'In review1 ticket',
        'Done2 tickets',
      ]);
      const done = screen.getByRole('region', { name: /^Done/ });
      expect(within(done).getByText('Closed issue')).toBeInTheDocument();
      expect(within(done).getByText('Done task')).toBeInTheDocument();
      expect(screen.queryByText('Cancelled one')).toBeNull();
      expect(screen.getByRole('region', { name: /^Cancelled/ })).toHaveTextContent('Not shown on the board');
    });

    it('says how to see finished tickets when All hides them', () => {
      localStorage.setItem('sfs-tickets-view', 'board');
      render(<TicketsTab projectId="proj_1" />);
      const done = screen.getByRole('region', { name: /^Done/ });
      expect(done).toHaveTextContent('Finished tickets are hidden. Turn on Show finished.');
    });

    it('cards carry the id, an Issue badge, priority, due date and assignee', () => {
      localStorage.setItem('sfs-tickets-view', 'board');
      hooks.useTickets.mockReturnValue({
        data: [ticket({ id: 'tk_i', title: 'An issue', status: 'open', kind: 'issue', priority: 'high', due_date: '2099-01-02' })],
        isLoading: false,
        error: null,
      });
      render(<TicketsTab projectId="proj_1" />);
      const card = screen.getByRole('button', { name: /An issue/ });
      expect(card).toHaveTextContent('tk_i');
      expect(card).toHaveTextContent('Issue');
      expect(card).toHaveTextContent('Priority High');
      expect(card).toHaveTextContent(/Due /);
      expect(card).toHaveTextContent('Assigned to atlas');
      expect(card.tabIndex).toBe(0);
    });
  });

  describe('new ticket dialog', () => {
    it('focuses the title, keeps the extra fields behind More details, and creates with ⌘↵', async () => {
      const user = userEvent.setup();
      const created = ticket({ id: 'tk_new', title: 'Ship it', status: 'open' });
      const create = makeMutation({ mutateAsync: vi.fn().mockResolvedValue(created) });
      hooks.useCreateTicket.mockReturnValue(create);
      hooks.useTicket.mockReturnValue({ data: created });
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.keyDown(document.body, { key: 'c' });
      const dialog = screen.getByRole('dialog', { name: 'New ticket' });
      const title = within(dialog).getByLabelText('Title');
      expect(title).toHaveFocus();
      const more = within(dialog).getByText(/^More details/).closest('details')!;
      expect(more.open).toBe(false);
      expect(more).toContainElement(within(dialog).getByLabelText('Due date'));
      await user.type(title, 'Ship it');
      await user.type(within(dialog).getByLabelText('Description'), 'Body');
      fireEvent.keyDown(within(dialog).getByLabelText('Description'), { key: 'Enter', metaKey: true });
      await waitFor(() => expect(create.mutateAsync).toHaveBeenCalledTimes(1));
      expect(create.mutateAsync.mock.calls[0][0]).toEqual(
        expect.objectContaining({ title: 'Ship it', description: 'Body', kind: 'task', priority: 'medium' }),
      );
      // The new ticket opens in the pane.
      await waitFor(() => expect(screen.getByRole('dialog', { name: 'Ship it' })).toBeInTheDocument());
    });

    it('creates with Ctrl+Enter too, and not without a title', async () => {
      const create = makeMutation({ mutateAsync: vi.fn().mockResolvedValue(ticket({ id: 'tk_new' })) });
      hooks.useCreateTicket.mockReturnValue(create);
      render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
      fireEvent.click(screen.getByRole('button', { name: /New ticket/ }));
      const dialog = screen.getByRole('dialog', { name: 'New ticket' });
      fireEvent.keyDown(within(dialog).getByLabelText('Title'), { key: 'Enter', ctrlKey: true });
      expect(create.mutateAsync).not.toHaveBeenCalled();
      fireEvent.change(within(dialog).getByLabelText('Title'), { target: { value: 'T' } });
      fireEvent.change(within(dialog).getByLabelText('Description'), { target: { value: 'D' } });
      fireEvent.keyDown(within(dialog).getByLabelText('Title'), { key: 'Enter', ctrlKey: true });
      await waitFor(() => expect(create.mutateAsync).toHaveBeenCalledTimes(1));
    });
  });
});
