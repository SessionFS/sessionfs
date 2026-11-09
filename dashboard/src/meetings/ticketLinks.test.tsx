import type { ReactNode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type MeetingActionItem, type MeetingDetail } from '../api/client';
import { axe } from '../test/axe';
import { meetingActionError } from './format';
import { LinkTicketDialog } from './LinkTicketDialog';
import { MeetingDetailView } from './MeetingDetailView';

/**
 * Existing-ticket suggestions and links on conversation items (0.29.11):
 * Link / Not this one / Create new on a suggestion, the "Link to ticket…"
 * picker, the linked state and Unlink. Hooks run for real against a fake
 * API client.
 */

const client = {
  getMeeting: vi.fn(),
  extractMeeting: vi.fn(),
  deleteMeeting: vi.fn(),
  resolveMeetingParticipant: vi.fn(),
  previewMeetingPromote: vi.fn(),
  promoteMeetingItem: vi.fn(),
  dismissMeetingItem: vi.fn(),
  restoreMeetingItem: vi.fn(),
  previewMeetingLink: vi.fn(),
  linkMeetingItem: vi.fn(),
  unlinkMeetingItem: vi.fn(),
  dismissMeetingSuggestion: vi.fn(),
  listLinkableTickets: vi.fn(),
  listAssignableUsers: vi.fn(),
  listPersonas: vi.fn(),
  listOrgPersonas: vi.fn(),
  getTicketById: vi.fn(),
  listJiraSprints: vi.fn(),
  listReminders: vi.fn(),
  listMeetingReminders: vi.fn(),
};
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));
vi.mock('../hooks/useMe', () => ({ useMe: () => ({ data: { user_id: 'u1', email: 'ola@example.test' } }) }));

const MEETING_ID = 'meet_0123456789abcdef';
const SUGGESTED = { id: 'tk_00000000000000s1', project_id: 'proj_1', title: 'Rollout plan for the launch', status: 'in_progress' };
const OTHER = { id: 'tk_00000000000000o2', project_id: 'proj_1', title: 'Status page copy', status: 'done' };

function item(overrides: Partial<MeetingActionItem> = {}): MeetingActionItem {
  return {
    id: 'mai_1',
    kind: 'action',
    description: 'Draft the rollout plan',
    owner_participant_id: null,
    due_date: null,
    line_start: 3,
    line_end: 4,
    status: 'pending',
    promoted_ticket_id: null,
    extracted_at: '2026-10-27T10:01:00Z',
    ...overrides,
  };
}

function detail(items: MeetingActionItem[]): MeetingDetail {
  return {
    id: MEETING_ID,
    org_id: 'org_1',
    project_id: 'proj_1',
    group_id: null,
    title: 'Weekly platform sync',
    meeting_at: '2026-10-27T00:00:00Z',
    duration_minutes: 30,
    source: 'manual_paste',
    line_count: 4,
    dlp_findings: 0,
    extraction_status: 'done',
    created_by_user_id: 'u1',
    created_at: '2026-10-27T10:00:00Z',
    updated_at: '2026-10-27T10:00:00Z',
    deleted_at: null,
    content: ['Priya: launch', 'Sam: ok', 'Priya: Sam, the rollout plan?', 'Sam: by Friday'].join('\n'),
    content_sha256: 'abc',
    participants: [],
    action_items: items,
    warnings: [],
  };
}

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function view(canEdit = true) {
  return (
    <MeetingDetailView
      meetingId={MEETING_ID}
      scope={{ kind: 'project', id: 'proj_1' }}
      orgId="org_1"
      canEdit={canEdit}
      canAdmin={false}
      lines={null}
      onLinesChange={vi.fn()}
      onBack={vi.fn()}
    />
  );
}

function apiError(status: number, code: string): ApiError {
  return new ApiError(status, JSON.stringify({ error: { code, message: 'Error', details: { code } } }));
}

beforeEach(() => {
  vi.clearAllMocks();
  client.listAssignableUsers.mockResolvedValue({ users: [] });
  client.listPersonas.mockResolvedValue([]);
  client.listOrgPersonas.mockResolvedValue([]);
  client.listLinkableTickets.mockResolvedValue({ tickets: [SUGGESTED, OTHER] });
});

describe('suggested ticket on an item', () => {
  it('shows "Looks like" with the ticket, and Link ties the item in one click', async () => {
    const suggested = item({ suggested_ticket: SUGGESTED, suggestion_source: 'model' });
    const linked = item({ status: 'linked', linked_ticket: SUGGESTED, linked_at: '2026-11-07T10:00:00Z' });
    client.getMeeting.mockResolvedValue(detail([suggested]));
    client.linkMeetingItem.mockImplementation(async () => {
      client.getMeeting.mockResolvedValue(detail([linked]));
      return { ticket_id: SUGGESTED.id, item_id: 'mai_1', status: 'linked', comment_id: 'tc_1', item: linked };
    });
    const { container } = wrap(view());
    const user = userEvent.setup();

    const row = (await screen.findByText('Looks like')).closest('div')!;
    expect(within(row).getByRole('link', { name: SUGGESTED.id })).toHaveAttribute(
      'href',
      `/projects/proj_1?tab=work&sub=tickets&ticket=${SUGGESTED.id}`,
    );
    expect(row).toHaveTextContent('Rollout plan for the launch');
    expect(row).toHaveTextContent('In progress');
    // Create new lives on the suggestion; no second "Create ticket".
    expect(screen.getByRole('button', { name: 'Create new ticket: Draft the rollout plan' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create ticket: Draft the rollout plan' })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    await user.click(screen.getByRole('button', { name: `Link to ${SUGGESTED.id}: Draft the rollout plan` }));
    await waitFor(() =>
      expect(client.linkMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_1', SUGGESTED.id),
    );
    expect(addToast).toHaveBeenCalledWith('success', `Linked to ${SUGGESTED.id}`);
    expect(await screen.findByText('Linked to')).toBeInTheDocument();
    expect(screen.queryByText('Looks like')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unlink: Draft the rollout plan' })).toBeInTheDocument();
  });

  it('"Not this one" hides the suggestion and keeps the item pending', async () => {
    const suggested = item({ suggested_ticket: SUGGESTED, suggestion_source: 'text' });
    client.getMeeting.mockResolvedValue(detail([suggested]));
    client.dismissMeetingSuggestion.mockImplementation(async () => {
      const plain = item({ suggested_ticket: null });
      client.getMeeting.mockResolvedValue(detail([plain]));
      return plain;
    });
    wrap(view());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: `Not this one: ${SUGGESTED.id} for Draft the rollout plan` }));
    await waitFor(() => expect(client.dismissMeetingSuggestion).toHaveBeenCalledWith(MEETING_ID, 'mai_1'));
    await waitFor(() => expect(screen.queryByText('Looks like')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Create ticket: Draft the rollout plan' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Link to ticket: Draft the rollout plan' })).toBeInTheDocument();
  });

  it('"Create new" opens the promote draft', async () => {
    client.getMeeting.mockResolvedValue(detail([item({ suggested_ticket: SUGGESTED })]));
    client.previewMeetingPromote.mockReturnValue(new Promise(() => {}));
    wrap(view());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create new ticket: Draft the rollout plan' }));
    expect(await screen.findByRole('dialog', { name: 'Create ticket' })).toBeInTheDocument();
    expect(client.previewMeetingPromote).toHaveBeenCalledWith(MEETING_ID, 'mai_1', undefined);
  });

  it('readers see the suggestion but no actions', async () => {
    client.getMeeting.mockResolvedValue(detail([item({ suggested_ticket: SUGGESTED })]));
    wrap(view(false));
    expect(await screen.findByText('Looks like')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Link to / })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Not this one/ })).not.toBeInTheDocument();
  });

  it('a decision with a suggestion offers Link and Not this one, never Create new', async () => {
    client.getMeeting.mockResolvedValue(
      detail([item({ id: 'mai_d', kind: 'decision', description: 'Launch on Nov 10', suggested_ticket: OTHER })]),
    );
    wrap(view());
    expect(await screen.findByRole('button', { name: `Link to ${OTHER.id}: Launch on Nov 10` })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Create new/ })).not.toBeInTheDocument();
  });
});

describe('linked items', () => {
  it('Unlink puts the item back to pending', async () => {
    client.getMeeting.mockResolvedValue(detail([item({ status: 'linked', linked_ticket: OTHER })]));
    client.unlinkMeetingItem.mockImplementation(async () => {
      const pending = item();
      client.getMeeting.mockResolvedValue(detail([pending]));
      return pending;
    });
    wrap(view());
    const user = userEvent.setup();
    expect(await screen.findByRole('link', { name: OTHER.id })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Unlink: Draft the rollout plan' }));
    await waitFor(() => expect(client.unlinkMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_1'));
    expect(await screen.findByRole('button', { name: 'Create ticket: Draft the rollout plan' })).toBeInTheDocument();
  });

  it('never names a linked ticket the caller cannot read', async () => {
    client.getMeeting.mockResolvedValue(detail([item({ status: 'linked', linked_ticket: null })]));
    wrap(view());
    expect(await screen.findByText('a ticket you can’t see')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /^tk_/ })).not.toBeInTheDocument();
  });
});

describe('LinkTicketDialog (Link to ticket…)', () => {
  it('searches, shows the comment it will add, links the chosen ticket and closes', async () => {
    client.previewMeetingLink.mockResolvedValue({
      meeting_id: MEETING_ID,
      item_id: 'mai_1',
      ticket: OTHER,
      suggested: false,
      comment: 'Raised again in meeting "Weekly platform sync" on 2026-10-27, lines L3–4: Draft the rollout plan',
    });
    let finish: (v: unknown) => void = () => {};
    client.linkMeetingItem.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const onClose = vi.fn();
    const { container } = wrap(<LinkTicketDialog meetingId={MEETING_ID} item={item()} onClose={onClose} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Ticket' }));
    await user.type(screen.getByRole('combobox', { name: 'Search ticket' }), 'status');
    await waitFor(() =>
      expect(client.listLinkableTickets).toHaveBeenLastCalledWith(MEETING_ID, { q: 'status', limit: 20 }),
    );
    await user.click(await screen.findByRole('option', { name: /Status page copy/ }));
    expect(await screen.findByText(/Raised again in meeting "Weekly platform sync"/)).toBeInTheDocument();
    expect(client.previewMeetingLink).toHaveBeenCalledWith(MEETING_ID, 'mai_1', OTHER.id);
    expect(await axe(container)).toHaveNoViolations();

    await user.click(screen.getByRole('button', { name: 'Link' }));
    // Locked while the link is written.
    expect(screen.getByRole('button', { name: 'Linking…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    await user.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();

    const linked = item({ status: 'linked', linked_ticket: OTHER });
    finish({ ticket_id: OTHER.id, item_id: 'mai_1', status: 'linked', comment_id: 'tc_1', item: linked });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(client.linkMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_1', OTHER.id);
    expect(addToast).toHaveBeenCalledWith('success', `Linked to ${OTHER.id}`);
  });

  it('explains a refused ticket and keeps the dialog open', async () => {
    client.previewMeetingLink.mockRejectedValue(apiError(422, 'ticket_not_linkable'));
    const onClose = vi.fn();
    wrap(<LinkTicketDialog meetingId={MEETING_ID} item={item()} onClose={onClose} chat />);
    const user = userEvent.setup();
    expect(screen.getByText(/citing messages 3–4/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Ticket' }));
    await user.click(await screen.findByRole('option', { name: /Rollout plan/ }));
    expect(await screen.findByText(/can’t be linked to this item/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('opens from "Link to ticket…" on an item with no suggestion', async () => {
    client.getMeeting.mockResolvedValue(detail([item()]));
    wrap(view());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Link to ticket: Draft the rollout plan' }));
    expect(await screen.findByRole('dialog', { name: 'Link to a ticket' })).toBeInTheDocument();
  });
});

describe('link error messages', () => {
  it('names each link refusal in plain words', () => {
    expect(meetingActionError(apiError(422, 'ticket_not_linkable'))).toMatch(/can’t be linked/);
    expect(meetingActionError(apiError(422, 'ticket_required'))).toMatch(/Choose the ticket/);
    expect(meetingActionError(apiError(409, 'item_not_linked'))).toMatch(/isn’t linked/);
  });
});
