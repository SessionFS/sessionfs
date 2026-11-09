import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TicketsTab from '../../TicketsTab';
import { axe } from '../../../test/axe';

/* The ticket pane, through the Tickets tab that hosts it: layouts, focus,
 * prev/next, tabs, deep links and the next-step actions. */

const { hooks, mockAddToast, mockUseMe, mockUseJiraLink, mockUsePersonas } = vi.hoisted(() => ({
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
    useTicketPreview: vi.fn(),
  },
  mockAddToast: vi.fn(),
  mockUseMe: vi.fn(),
}));

vi.mock('../../../reminders/RemindersStrip', () => ({ RemindersStrip: () => null }));
vi.mock('../../../hooks/useTickets', () => hooks);
vi.mock('../../../hooks/useJiraTickets', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../hooks/useJiraTickets')>()),
  useJiraLink: mockUseJiraLink,
}));
vi.mock('../../../hooks/useContacts', () => ({
  useActiveContacts: () => ({ data: { items: [], next_before: null }, isLoading: false, isError: false }),
}));
vi.mock('../../../hooks/useMe', () => ({ useMe: mockUseMe }));
vi.mock('../../../hooks/usePersonas', () => ({ usePersonas: mockUsePersonas }));
vi.mock('../../../hooks/useToast', () => ({ useToast: () => ({ addToast: mockAddToast }) }));
vi.mock('../../../hooks/useFocusTrap', () => ({ useFocusTrap: vi.fn() }));

function makeMutation(extra: Record<string, unknown> = {}) {
  return { mutate: vi.fn(), mutateAsync: vi.fn().mockResolvedValue(undefined), isPending: false, isError: false, error: null, ...extra };
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
    status: 'open',
    kind: 'task',
    parent_ticket_id: null,
    child_ticket_ids: [],
    context_refs: [],
    file_refs: [],
    related_sessions: [],
    acceptance_criteria: ['CLI works'],
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

const ADMIN = { canEdit: true, userId: 'u9', isProjectAdmin: true };
const TWO = [ticket(), ticket({ id: 'tk_2', title: 'Second ticket', status: 'suggested' })];

const REVIEW = {
  ticket_id: 'tk_1',
  review_state: {
    open_findings: [],
    closed_findings: [
      { severity: 'MEDIUM', text: 'Refetches on every tab switch.', round: 1, closed_round: 2 },
      { severity: 'LOW', text: 'Lost its heading id.', round: 1, closed_round: 2 },
    ],
    last_verdict: 'VERIFIED-CLEAN',
    rounds: [
      { round: 1, verdict: 'CHANGES_REQUESTED', raw_verdict: 'CHANGES_REQUESTED', is_strict_verified_clean: false, comment_id: 'c1', timestamp: '2026-05-14T02:00:00Z', findings_raised: 2 },
      { round: 2, verdict: 'VERIFIED-CLEAN', raw_verdict: 'VERIFIED-CLEAN', is_strict_verified_clean: true, comment_id: 'c2', timestamp: '2026-05-14T03:00:00Z', findings_raised: 0 },
    ],
  },
};

function setMedia(match: (q: string) => boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: match(query),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}
const wide = () => setMedia((q) => q === '(min-width: 1360px)');
const phone = () => setMedia((q) => q === '(max-width: 719px)');

const defaultMatchMedia = window.matchMedia;

beforeEach(() => {
  hooks.useTickets.mockReturnValue({ data: TWO, isLoading: false, error: null });
  hooks.useTicket.mockReturnValue({ data: undefined });
  hooks.useTicketChildren.mockReturnValue([]);
  hooks.useTicketComments.mockReturnValue({ data: [] });
  hooks.useTicketJiraSyncEvents.mockReturnValue({ data: { events: [] } });
  hooks.useTicketEvents.mockReturnValue({ data: { pages: [{ events: [], next_before: null }] }, hasNextPage: false, isFetchingNextPage: false, fetchNextPage: vi.fn() });
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
  hooks.useAssignableUsers.mockReturnValue({ data: [], isLoading: false });
  hooks.useTicketPreview.mockReturnValue({ data: undefined });
  mockUsePersonas.mockReturnValue({ data: [], isLoading: false });
  mockUseMe.mockReturnValue({ data: { user_id: 'u1' }, isLoading: false });
  mockUseJiraLink.mockReturnValue({ data: undefined, refetch: vi.fn() });
});

afterEach(() => {
  vi.clearAllMocks();
  window.matchMedia = defaultMatchMedia;
  localStorage.removeItem('sfs-tickets-view');
});

function rowButton(title: string) {
  return screen.getAllByText(title).map((el) => el.closest('button')!).find((b) => b.hasAttribute('aria-expanded'))!;
}

describe('TicketPane layouts', () => {
  it('split pane: Escape closes it and focus returns to the row', async () => {
    wide();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    const row = rowButton('Add session export');
    row.focus();
    fireEvent.click(row);
    const pane = screen.getByRole('region', { name: 'Add session export' });
    expect(pane).toHaveAttribute('data-ticket-pane', 'split');
    fireEvent.keyDown(within(pane).getByRole('heading', { name: 'Add session export' }), { key: 'Escape' });
    expect(screen.queryByRole('region', { name: 'Add session export' })).toBeNull();
    await waitFor(() => expect(row).toHaveFocus());
  });

  it('split pane: the close button closes it', () => {
    wide();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    fireEvent.click(screen.getByRole('button', { name: 'Close ticket' }));
    expect(screen.queryByRole('region', { name: 'Add session export' })).toBeNull();
  });

  it('split pane: Escape in a half-typed comment does not close it', () => {
    wide();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    fireEvent.click(screen.getByRole('tab', { name: /^Activity/ }));
    const box = screen.getByPlaceholderText('Add comment…');
    fireEvent.change(box, { target: { value: 'Half a thought' } });
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(screen.getByRole('region', { name: 'Add session export' })).toBeInTheDocument();
  });

  it('board always uses the slide-over, even when wide', () => {
    wide();
    localStorage.setItem('sfs-tickets-view', 'board');
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(screen.getByText('Add session export'));
    expect(screen.getByRole('dialog', { name: 'Add session export' })).toHaveAttribute('aria-modal', 'true');
  });

  it('phone: full screen with a back button that closes it', () => {
    phone();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    const dialog = screen.getByRole('dialog', { name: 'Add session export' });
    expect(within(dialog).queryByRole('button', { name: 'Close ticket' })).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to tickets' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('steps to the next and previous ticket in list order', () => {
    wide();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    expect(screen.getByRole('button', { name: 'Previous ticket' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Next ticket' }));
    expect(screen.getByRole('region', { name: 'Second ticket' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next ticket' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Previous ticket' }));
    expect(screen.getByRole('region', { name: 'Add session export' })).toBeInTheDocument();
  });

  it('has no axe violations in the split pane and the slide-over', async () => {
    wide();
    const { container, unmount } = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    expect(await axe(container)).toHaveNoViolations();
    unmount();
    window.matchMedia = defaultMatchMedia;
    const second = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    expect(await axe(second.baseElement)).toHaveNoViolations();
  });
});

describe('TicketPane deep links', () => {
  it('opens the linked ticket on the linked tab', () => {
    hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'review' })], isLoading: false, error: null });
    hooks.useTicketReviewState.mockReturnValue({ data: REVIEW });
    render(<TicketsTab projectId="proj_1" initialTicketId="tk_1" initialTicketTab="review" />);
    expect(screen.getByRole('tab', { name: /^Review/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Verified clean, round 2')).toBeInTheDocument();
  });

  it('falls back to Overview for a tab the ticket does not have', () => {
    render(<TicketsTab projectId="proj_1" initialTicketId="tk_1" initialTicketTab="children" />);
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tab', { name: /Child tasks/ })).toBeNull();
  });

  it('loads a linked ticket the current filters hide', () => {
    hooks.useTicket.mockImplementation((_p: string, id?: string) =>
      id === 'tk_9' ? { data: ticket({ id: 'tk_9', title: 'Hidden by filters' }) } : { data: undefined },
    );
    render(<TicketsTab projectId="proj_1" initialTicketId="tk_9" />);
    expect(screen.getByRole('dialog', { name: 'Hidden by filters' })).toBeInTheDocument();
  });

  it('says it is loading, then explains a ticket that fails to load', () => {
    const { unmount } = render(<TicketsTab projectId="proj_1" initialTicketId="tk_missing" />);
    expect(screen.getByText('Loading ticket…')).toBeInTheDocument();
    unmount();
    hooks.useTicket.mockReturnValue({ data: undefined, isError: true, error: new Error('Not found') });
    render(<TicketsTab projectId="proj_1" initialTicketId="tk_missing" />);
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load this ticket");
  });

  it('opens a ticket when the deep link changes while mounted', () => {
    const { rerender } = render(<TicketsTab projectId="proj_1" initialTicketId={null} />);
    expect(screen.queryByRole('dialog')).toBeNull();
    rerender(<TicketsTab projectId="proj_1" initialTicketId="tk_2" />);
    expect(screen.getByRole('dialog', { name: 'Second ticket' })).toBeInTheDocument();
  });
});

describe('TicketPane URL state', () => {
  it('reports the open ticket and its tab to the page', () => {
    wide();
    const onOpen = vi.fn();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} onOpenTicketChange={onOpen} />);
    fireEvent.click(rowButton('Add session export'));
    expect(onOpen).toHaveBeenLastCalledWith('tk_1', null);
    expect(rowButton('Add session export')).toHaveAttribute('aria-current', 'true');
    fireEvent.click(screen.getByRole('tab', { name: /^Activity/ }));
    expect(onOpen).toHaveBeenLastCalledWith('tk_1', 'activity');
    fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
    expect(onOpen).toHaveBeenLastCalledWith('tk_1', null);
    fireEvent.click(screen.getByRole('button', { name: 'Close ticket' }));
    expect(onOpen).toHaveBeenLastCalledWith(null, null);
  });

  it('does not report the deep-linked ticket back on first render', () => {
    const onOpen = vi.fn();
    render(<TicketsTab projectId="proj_1" initialTicketId="tk_1" onOpenTicketChange={onOpen} />);
    expect(onOpen).not.toHaveBeenCalled();
  });
});

describe('TicketPane tabs', () => {
  it('shows the Review tab with rounds, findings and merge readiness', async () => {
    hooks.useTickets.mockReturnValue({ data: [ticket({ status: 'review' })], isLoading: false, error: null });
    hooks.useTicketReviewState.mockReturnValue({ data: REVIEW });
    const { baseElement } = render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    const next = screen.getByRole('region', { name: 'Next step' });
    expect(next).toHaveTextContent('Verified clean in round 2');
    expect(next).toHaveTextContent('sfs ticket complete tk_1');
    fireEvent.click(within(next).getByRole('button', { name: 'Open review' }));
    const tab = screen.getByRole('tab', { name: /^Review/ });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(tab).toHaveTextContent('R2 clean');
    const panel = screen.getByRole('tabpanel', { name: /^Review/ });
    expect(within(panel).getByText('Round 1')).toBeInTheDocument();
    expect(within(panel).getByText('0 open · 2 closed')).toBeInTheDocument();
    expect(within(panel).getByText('Refetches on every tab switch.')).toHaveClass('line-through');
    expect(within(panel).getAllByText('R1 → closed R2')).toHaveLength(2);
    expect(within(panel).getByText('Merge readiness')).toBeInTheDocument();
    // Product terms carry their glossary definitions.
    for (const name of ['Merge check', 'Verified clean', 'Trusted reviewer', 'Rounds']) {
      expect(within(panel).getByRole('button', { name })).toHaveAccessibleDescription(/./);
    }
    expect(within(next).getByRole('button', { name: 'trusted reviewer' })).toBeInTheDocument();
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('has no Review tab for a ticket without a review', () => {
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    expect(screen.queryByRole('tab', { name: /^Review/ })).toBeNull();
  });

  it('filters the activity to comments or history', () => {
    hooks.useTicketComments.mockReturnValue({
      data: [{ id: 'c1', ticket_id: 'tk_1', author_user_id: 'u1', author_persona: 'atlas', content: 'Real comment', session_id: null, created_at: '2026-05-14T02:00:00Z', source: 'sessionfs' }],
    });
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    const tab = screen.getByRole('tab', { name: /^Activity/ });
    expect(tab).toHaveTextContent('Activity1');
    fireEvent.click(tab);
    const activity = screen.getByRole('region', { name: 'Activity' });
    expect(within(activity).getByText(/^Created/)).toBeInTheDocument();
    fireEvent.click(within(activity).getByRole('button', { name: 'Comments' }));
    expect(within(activity).getByRole('button', { name: 'Comments' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(activity).queryByText(/^Created/)).toBeNull();
    expect(within(activity).getByText('Real comment')).toBeInTheDocument();
    fireEvent.click(within(activity).getByRole('button', { name: 'History' }));
    expect(within(activity).queryByText('Real comment')).toBeNull();
    expect(within(activity).getByText(/^Created/)).toBeInTheDocument();
  });

  it('posts a comment with Cmd+Enter', () => {
    const add = makeMutation();
    hooks.useAddTicketComment.mockReturnValue(add);
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    fireEvent.click(screen.getByRole('tab', { name: /^Activity/ }));
    const box = screen.getByPlaceholderText('Add comment…');
    fireEvent.change(box, { target: { value: 'Quick note' } });
    fireEvent.keyDown(box, { key: 'Enter', metaKey: true });
    expect(add.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', content: 'Quick note', sendToJira: undefined });
  });

  it('shows child tasks with a stacked progress bar and adds one under the Issue', async () => {
    const issue = ticket({ id: 'tk_issue', title: 'Sync stalls', kind: 'issue', status: 'in_progress', child_ticket_ids: ['tk_a', 'tk_b', 'tk_c'] });
    hooks.useTickets.mockReturnValue({ data: [issue], isLoading: false, error: null });
    hooks.useTicketChildren.mockImplementation((_p: string, ids: string[]) =>
      ids.map((id, i) => ({ data: ticket({ id, title: `Child ${id}`, status: ['done', 'blocked', 'review'][i] }), isError: false })),
    );
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Sync stalls'));
    const next = screen.getByRole('region', { name: 'Next step' });
    expect(next).toHaveTextContent('1 of 3 child tasks finished');
    expect(next).toHaveTextContent('1 child task is blocked');
    await user.click(within(next).getByRole('button', { name: 'See child tasks' }));
    const panel = screen.getByRole('tabpanel', { name: /^Child tasks/ });
    expect(within(panel).getByRole('img', { name: '1 finished, 1 in review, 1 blocked' })).toBeInTheDocument();
    expect(within(panel).getAllByRole('button', { name: /Child tk_/ })).toHaveLength(3);
    await user.click(within(panel).getByRole('button', { name: 'Add child task' }));
    expect(screen.getByRole('dialog', { name: 'New ticket' })).toBeInTheDocument();
  });

  it('shows the Jira tab when the project has a Jira surface', () => {
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} jira={{ orgId: 'org_1', isProjectAdmin: true, isEnterprise: false }} />);
    fireEvent.click(rowButton('Add session export'));
    fireEvent.click(screen.getByRole('tab', { name: /^Jira/ }));
    expect(screen.getByText('Promote to Jira is available on the Enterprise plan.')).toBeInTheDocument();
  });
});

describe('TicketPane next step and properties', () => {
  it('approves a suggested ticket from the next-step card', async () => {
    const approve = makeMutation();
    hooks.useApproveTicket.mockReturnValue(approve);
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Second ticket'));
    const next = screen.getByRole('region', { name: 'Next step' });
    await act(async () => {
      fireEvent.click(within(next).getByRole('button', { name: 'Approve' }));
    });
    expect(approve.mutateAsync).toHaveBeenCalledWith('tk_2');
    expect(mockAddToast).toHaveBeenCalledWith('success', 'Approved');
  });

  it('asks before dismissing from the next-step card', async () => {
    const dismiss = makeMutation();
    hooks.useDismissTicket.mockReturnValue(dismiss);
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Second ticket'));
    fireEvent.click(within(screen.getByRole('region', { name: 'Next step' })).getByRole('button', { name: /Dismiss/ }));
    expect(dismiss.mutateAsync).not.toHaveBeenCalled();
    const confirm = screen.getByRole('dialog', { name: 'Dismiss this ticket?' });
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Dismiss ticket' }));
    });
    expect(dismiss.mutateAsync).toHaveBeenCalledWith('tk_2');
  });

  it('names the blocker and opens it', () => {
    const blocked = ticket({ status: 'blocked', depends_on: ['tk_2'] });
    hooks.useTickets.mockReturnValue({ data: [blocked, TWO[1]], isLoading: false, error: null });
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    const next = screen.getByRole('region', { name: 'Next step' });
    expect(next).toHaveTextContent('Waiting on tk_2');
    expect(next).toHaveTextContent('Second ticket is suggested.');
    // The dependency row shows its live status.
    expect(screen.getByRole('button', { name: /Suggested:.*tk_2.*Second ticket/ })).toBeInTheDocument();
    fireEvent.click(within(next).getByRole('button', { name: 'Open blocker' }));
    expect(screen.getByRole('dialog', { name: 'Second ticket' })).toBeInTheDocument();
  });

  it('explains that work starts from the agent and offers no state change', () => {
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    const next = screen.getByRole('region', { name: 'Next step' });
    expect(next).toHaveTextContent('Ready for atlas to start');
    expect(within(next).queryAllByRole('button')).toHaveLength(0);
  });

  it('changes priority from the header for the creator or an admin', async () => {
    const update = makeMutation();
    hooks.useUpdateTicket.mockReturnValue(update);
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    await user.click(screen.getByRole('button', { name: 'Priority: medium. Change priority' }));
    await user.click(screen.getByRole('menuitem', { name: 'High' }));
    expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { priority: 'high' } });
  });

  it('asks before a non-editor priority change sends an approved ticket back', async () => {
    const update = makeMutation();
    hooks.useUpdateTicket.mockReturnValue(update);
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u1' }} />);
    fireEvent.click(rowButton('Add session export'));
    await user.click(screen.getByRole('button', { name: 'Priority: medium. Change priority' }));
    await user.click(screen.getByRole('menuitem', { name: 'Critical' }));
    expect(update.mutateAsync).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Save and send back for approval' }));
    expect(update.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', body: { priority: 'critical' } });
  });

  it('Escape on the status menu closes the menu, not the slide-over', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Second ticket'));
    const dialog = screen.getByRole('dialog', { name: 'Second ticket' });
    await user.click(within(dialog).getByRole('button', { name: /Show status actions/ }));
    expect(within(dialog).getByRole('menu', { name: 'Status actions for tk_2' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(within(dialog).queryByRole('menu')).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Second ticket' })).toBeInTheDocument();
  });

  it('shows priority as text to a reader', () => {
    render(<TicketsTab projectId="proj_1" viewer={{ canEdit: false, userId: 'u9' }} />);
    fireEvent.click(rowButton('Add session export'));
    expect(screen.queryByRole('button', { name: /Change priority/ })).toBeNull();
  });

  it('opens the due date editor from the header property', async () => {
    const user = userEvent.setup();
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    fireEvent.click(screen.getByRole('tab', { name: /^Activity/ }));
    await user.click(screen.getByRole('button', { name: 'Add due date' }));
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(screen.getByLabelText('Due date')).toHaveFocus());
  });

  it('copies the ticket id', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Copy ticket id' }));
    });
    expect(writeText).toHaveBeenCalledWith('tk_1');
    expect(mockAddToast).toHaveBeenCalledWith('success', 'Copied tk_1');
  });

  it('follows from the header bell', async () => {
    const watch = makeMutation();
    hooks.useWatchTicket.mockReturnValue(watch);
    render(<TicketsTab projectId="proj_1" viewer={ADMIN} />);
    fireEvent.click(rowButton('Add session export'));
    const bell = screen.getByRole('button', { name: 'Follow this ticket' });
    expect(bell).toHaveAttribute('aria-pressed', 'false');
    await act(async () => {
      fireEvent.click(bell);
    });
    expect(watch.mutateAsync).toHaveBeenCalledWith({ ticketId: 'tk_1', muted: false });
  });
});
