import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TicketsTab from '../TicketsTab';
import { axe } from '../../test/axe';

const { hooks, mockAddToast } = vi.hoisted(() => ({
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
}));

vi.mock('../../reminders/RemindersStrip', () => ({ RemindersStrip: () => null }));
vi.mock('../../hooks/useTickets', () => hooks);
vi.mock('../../hooks/useJiraTickets', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/useJiraTickets')>()),
  useJiraLink: () => ({ data: undefined, refetch: vi.fn() }),
}));
vi.mock('../../hooks/useContacts', () => ({
  useActiveContacts: () => ({ data: { items: [], next_before: null }, isLoading: false, isError: false }),
}));
vi.mock('../../hooks/useMe', () => ({ useMe: () => ({ data: { user_id: 'u1' }, isLoading: false }) }));
vi.mock('../../hooks/usePersonas', () => ({
  usePersonas: () => ({
    data: [
      {
        id: 'pers_1', project_id: 'proj_1', org_id: null, scope: 'project', name: 'atlas', role: 'Backend',
        content: '', specializations: [], is_active: true, version: 1, created_by: 'u1', represented_user_id: null,
        created_at: '2026-05-14T00:00:00Z', updated_at: '2026-05-14T00:00:00Z',
      },
    ],
    isLoading: false,
  }),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast: mockAddToast }) }));
vi.mock('../../hooks/useFocusTrap', () => ({ useFocusTrap: vi.fn() }));

function mutation() {
  return { mutate: vi.fn(), mutateAsync: vi.fn().mockResolvedValue(undefined), isPending: false, isError: false, error: null };
}

function ticket(over: Record<string, unknown> = {}) {
  return {
    id: 'tk_1', project_id: 'proj_1', title: 'Add session export', description: 'Body.', priority: 'medium',
    assigned_to: 'atlas', created_by_user_id: 'u1', created_by_session_id: null, created_by_persona: null,
    status: 'suggested', kind: 'task', parent_ticket_id: null, child_ticket_ids: [], context_refs: [], file_refs: [],
    related_sessions: [], acceptance_criteria: [], resolver_session_id: null, resolver_user_id: null,
    completion_notes: null, changed_files: [], knowledge_entry_ids: [], depends_on: [],
    created_at: '2026-05-14T00:00:00Z', updated_at: '2026-05-14T01:00:00Z', resolved_at: null,
    ...over,
  };
}

const EDITOR = { canEdit: true, userId: 'u9', isProjectAdmin: true };
let approve: ReturnType<typeof mutation>;
let dismiss: ReturnType<typeof mutation>;
let close: ReturnType<typeof mutation>;
let update: ReturnType<typeof mutation>;

beforeEach(() => {
  approve = mutation();
  dismiss = mutation();
  close = mutation();
  update = mutation();
  hooks.useTickets.mockReturnValue({ data: [ticket()], isLoading: false, error: null, refetch: vi.fn() });
  hooks.useTicket.mockReturnValue({ data: undefined });
  hooks.useTicketChildren.mockReturnValue([]);
  hooks.useTicketComments.mockReturnValue({ data: [] });
  hooks.useTicketJiraSyncEvents.mockReturnValue({ data: { events: [] } });
  hooks.useTicketEvents.mockReturnValue({ data: { pages: [{ events: [], next_before: null }], pageParams: [''] }, hasNextPage: false, isFetchingNextPage: false, fetchNextPage: vi.fn() });
  hooks.useTicketReviewState.mockReturnValue({ data: undefined });
  hooks.useCreateTicket.mockReturnValue(mutation());
  hooks.useApproveTicket.mockReturnValue(approve);
  hooks.useDismissTicket.mockReturnValue(dismiss);
  hooks.useCloseTicket.mockReturnValue(close);
  hooks.useAddTicketComment.mockReturnValue(mutation());
  hooks.useTicketWatchers.mockReturnValue({ data: [] });
  hooks.useWatchTicket.mockReturnValue(mutation());
  hooks.useUnwatchTicket.mockReturnValue(mutation());
  hooks.useUpdateTicket.mockReturnValue(update);
  hooks.useAssignableUsers.mockReturnValue({
    data: [
      { id: 'u1', display_name: 'Ola Dev', email: 'ola@example.test', can_edit: true },
      { id: 'u2', display_name: 'Sam Rivera', email: 'sam@example.test', can_edit: true },
    ],
    isLoading: false,
  });
});

afterEach(() => vi.clearAllMocks());

const statusButton = (name: RegExp) => screen.getByRole('button', { name });

describe('status menu', () => {
  it('offers approve and dismiss on a suggested ticket, plus where the other moves happen', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    await user.click(statusButton(/^Status: Suggested/));
    const menu = screen.getByRole('menu', { name: 'Status actions for tk_1' });
    expect(within(menu).getAllByRole('menuitem').map((m) => m.textContent)).toEqual(['Approveto Open', 'Dismiss…to Cancelled']);
    expect(screen.getByText(/Start, complete, block and resolve happen where the work runs/)).toBeInTheDocument();
    // Focus starts on the first item; arrows move.
    expect(within(menu).getByRole('menuitem', { name: /Approve/ })).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(within(menu).getByRole('menuitem', { name: /Dismiss/ })).toHaveFocus();
    await user.click(within(menu).getByRole('menuitem', { name: /Approve/ }));
    expect(approve.mutateAsync).toHaveBeenCalledWith('tk_1');
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('asks before a dismiss', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    await user.click(statusButton(/^Status: Suggested/));
    await user.click(screen.getByRole('menuitem', { name: /Dismiss/ }));
    expect(dismiss.mutateAsync).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog', { name: 'Dismiss this ticket?' });
    await user.click(within(dialog).getByRole('button', { name: 'Keep it' }));
    expect(dismiss.mutateAsync).not.toHaveBeenCalled();
    await user.click(statusButton(/^Status: Suggested/));
    await user.click(screen.getByRole('menuitem', { name: /Dismiss/ }));
    await user.click(screen.getByRole('button', { name: 'Dismiss ticket' }));
    expect(dismiss.mutateAsync).toHaveBeenCalledWith('tk_1');
  });

  it('offers Close issue only on an in-progress Issue', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket({ kind: 'issue', status: 'in_progress' }), ticket({ id: 'tk_2', title: 'Task', status: 'in_progress' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    const [issueBtn, taskBtn] = screen.getAllByRole('button', { name: /^Status: In progress/ });
    await user.click(issueBtn);
    await user.click(screen.getByRole('menuitem', { name: /Close issue/ }));
    expect(close.mutateAsync).toHaveBeenCalledWith('tk_1');
    await user.click(taskBtn);
    expect(screen.queryByRole('menuitem')).toBeNull();
    expect(screen.getByText('No status change from here.')).toBeInTheDocument();
  });

  it('shows no moves to a reader who may not approve; Escape closes and returns focus', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u7' }} />);
    const btn = statusButton(/^Status: Suggested/);
    await user.click(btn);
    expect(screen.queryByRole('menuitem')).toBeNull();
    await user.keyboard('{Escape}');
    expect(screen.queryByText('No status change from here.')).toBeNull();
    expect(btn).toHaveFocus();
  });

  it('has no axe violations with the menu open', async () => {
    const user = userEvent.setup();
    const { container } = render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    await user.click(statusButton(/^Status: Suggested/));
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('assignee menu', () => {
  it('reassigns from the row', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    await user.click(screen.getByRole('button', { name: 'Assignee: atlas (agent). Reassign' }));
    const panel = screen.getByRole('dialog', { name: 'Reassign tk_1' });
    await user.click(within(panel).getByRole('button', { name: 'Assignee' }));
    await user.click(screen.getByRole('option', { name: /Sam Rivera/ }));
    await user.click(within(panel).getByRole('button', { name: 'Save' }));
    expect(update.mutateAsync).toHaveBeenCalledWith({
      ticketId: 'tk_1',
      body: { assigned_user_id: 'u2', assigned_to: null },
    });
  });

  it("asks before a non-editor's change sends an approved ticket back", async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'open' })], isLoading: false, error: null });
    const { container } = render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
    await user.click(screen.getByRole('button', { name: 'Assignee: atlas (agent). Reassign' }));
    const panel = screen.getByRole('dialog', { name: 'Reassign tk_1' });
    await user.click(within(panel).getByRole('button', { name: 'Unassign' }));
    expect(update.mutateAsync).not.toHaveBeenCalled();
    expect(screen.getByText(/Saving this sends the ticket back for approval/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
    await user.click(screen.getByRole('button', { name: 'Save and send back for approval' }));
    expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { assigned_to: null, assigned_user_id: null } });
  });

  it('is plain text for a reader who may not reassign', () => {
    render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u7' }} />);
    expect(screen.queryByRole('button', { name: /Reassign/ })).toBeNull();
    expect(screen.getByTitle('Agent: atlas')).toBeInTheDocument();
  });
});

describe('list', () => {
  it('folds finished tickets in All and shows them on request', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [ticket({ status: 'open' }), ticket({ id: 'tk_2', title: 'Shipped', status: 'done' })],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    expect(screen.queryByText('Shipped')).toBeNull();
    await user.click(screen.getByRole('button', { name: /1 finished ticket hidden/ }));
    expect(screen.getByText('Shipped')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show finished' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('groups by Issue and collapses a group', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({ id: 'tk_i', title: 'Roll-up', kind: 'issue', status: 'in_progress', child_ticket_ids: ['tk_c'], child_counts: { total: 1, finished: 0 } }),
        ticket({ id: 'tk_c', title: 'Child task', status: 'open', parent_ticket_id: 'tk_i' }),
        ticket({ id: 'tk_l', title: 'Loose task', status: 'open' }),
      ],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    await user.click(screen.getByRole('button', { name: 'Group by' }));
    await user.click(screen.getByRole('option', { name: 'Issue' }));
    const head = within(screen.getByRole('heading', { name: /^Roll-up/ })).getByRole('button');
    expect(head).toHaveTextContent('0/1 done');
    expect(within(screen.getByRole('list', { name: 'Roll-up' })).getByText('Child task')).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: 'Not in an Issue' })).getByText('Loose task')).toBeInTheDocument();
    await user.click(head);
    expect(head).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Child task')).toBeNull();
  });

  it('sorts by due date inside a group', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({ id: 'tk_a', title: 'Later', status: 'open', priority: 'critical', due_date: '2999-02-01' }),
        ticket({ id: 'tk_b', title: 'Sooner', status: 'open', priority: 'low', due_date: '2999-01-01' }),
      ],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    const titles = () => screen.getAllByRole('button', { name: /^(Later|Sooner)$/ }).map((b) => b.textContent);
    expect(titles()).toEqual(['Later', 'Sooner']);
    await user.click(screen.getByRole('button', { name: 'Sort by' }));
    await user.click(screen.getByRole('option', { name: 'Due date' }));
    expect(titles()).toEqual(['Sooner', 'Later']);
  });

  it('shows skeleton rows while loading and a retry on error', async () => {
    const user = userEvent.setup();
    hooks.useTickets.mockReturnValue({ data: undefined, isLoading: true, error: null, refetch: vi.fn() });
    const { unmount } = render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByLabelText('Loading tickets')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('tab', { name: /^All/ })).toBeInTheDocument();
    unmount();
    const refetch = vi.fn();
    hooks.useTickets.mockReturnValue({ data: undefined, isLoading: false, error: new Error('boom'), refetch });
    render(<TicketsTab projectId="proj_1" />);
    expect(screen.getByText(/Couldn’t load tickets: boom/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(refetch).toHaveBeenCalled();
  });

  it('keeps a deep-linked finished ticket open without unfolding the rest', () => {
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({ id: 'tk_2', title: 'Shipped', status: 'done' }),
        ticket({ id: 'tk_3', title: 'Also shipped', status: 'done' }),
      ],
      isLoading: false,
      error: null,
    });
    render(<TicketsTab projectId="proj_1" viewer={EDITOR} initialTicketId="tk_2" />);
    expect(screen.getByRole('button', { name: 'Shipped' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.queryByText('Also shipped')).toBeNull();
  });

  it('has no axe violations on a list with every signal', async () => {
    hooks.useTickets.mockReturnValue({
      data: [
        ticket({ id: 'tk_r', status: 'review', review_summary: { last_round: 2, last_verdict: 'clean', open_findings: 0, trusted: true }, jira_key: 'WID-1' }),
        ticket({ id: 'tk_b', status: 'blocked', open_dependency_count: 2, due_date: '2026-01-01', priority: 'critical' }),
        ticket({ id: 'tk_i', kind: 'issue', status: 'in_progress', child_counts: { total: 3, finished: 1 }, assigned_to: null, assigned_user_id: 'u1', assigned_user: { id: 'u1', display_name: 'Ola Dev', email: 'ola@example.test' } }),
      ],
      isLoading: false,
      error: null,
    });
    const { container } = render(<TicketsTab projectId="proj_1" viewer={EDITOR} />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
