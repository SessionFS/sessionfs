import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TicketsTab from './TicketsTab';

const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    useTickets: vi.fn(),
    useTicket: vi.fn(),
    useTicketChildren: vi.fn(),
    useTicketComments: vi.fn(),
    useCreateTicket: vi.fn(),
    useApproveTicket: vi.fn(),
    useDismissTicket: vi.fn(),
    useCloseTicket: vi.fn(),
    useAddTicketComment: vi.fn(),
  },
  mockAddToast: vi.fn(),
}));

vi.mock('../hooks/useTickets', () => hooks);
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

beforeEach(() => {
  hooks.useTickets.mockReturnValue({ data: [ticket()], isLoading: false, error: null });
  hooks.useTicket.mockReturnValue({ data: undefined });
  hooks.useTicketChildren.mockReturnValue([]);
  hooks.useTicketComments.mockReturnValue({ data: [] });
  hooks.useCreateTicket.mockReturnValue(makeMutation());
  hooks.useApproveTicket.mockReturnValue(makeMutation());
  hooks.useDismissTicket.mockReturnValue(makeMutation());
  hooks.useCloseTicket.mockReturnValue(makeMutation());
  hooks.useAddTicketComment.mockReturnValue(makeMutation());
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
    await user.click(screen.getByRole('combobox', { name: /Filter by status/i }));
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
    await user.click(screen.getByRole('combobox', { name: /Filter by kind/i }));
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
    await user.click(screen.getByRole('combobox', { name: /Filter by kind/i }));
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
    const kindTrigger = screen.getByRole('combobox', { name: 'Kind' });
    expect(kindTrigger).toHaveTextContent('Task');
    // Parent Issue picker is visible when kind is task
    expect(screen.getByRole('combobox', { name: /Parent Issue/ })).toBeInTheDocument();
    // Switch to Issue
    await user.click(kindTrigger);
    await user.click(
      screen.getByRole('option', { name: 'Issue' }),
    );
    // Parent Issue picker hidden for Issues
    expect(screen.queryByRole('combobox', { name: /Parent Issue/ })).toBeNull();
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
});
