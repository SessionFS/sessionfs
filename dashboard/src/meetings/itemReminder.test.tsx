import type { ReactNode } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type MeetingDetail } from '../api/client';
import { axe } from '../test/axe';
import { MeetingDetailView } from './MeetingDetailView';

const client = {
  getMeeting: vi.fn(),
  createMeetingItemReminder: vi.fn(),
  listMeetingReminders: vi.fn(),
  listAssignableUsers: vi.fn(),
  listPersonas: vi.fn(),
  listOrgPersonas: vi.fn(),
};
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));
vi.mock('../hooks/useMe', () => ({ useMe: () => ({ data: { user_id: 'u1', email: 'ola@example.test' } }) }));

const MEETING_ID = 'meet_0123456789abcdef';

function detail(): MeetingDetail {
  return {
    id: MEETING_ID,
    org_id: 'org_1',
    project_id: 'proj_1',
    group_id: null,
    title: 'Weekly platform sync',
    meeting_at: '2026-10-27T00:00:00Z',
    duration_minutes: 30,
    source: 'manual_paste',
    line_count: 3,
    dlp_findings: 0,
    extraction_status: 'done',
    created_by_user_id: 'u1',
    created_at: '2026-10-27T10:00:00Z',
    updated_at: '2026-10-27T10:00:00Z',
    deleted_at: null,
    content: 'Priya: a\nSam: b\nPriya: c',
    content_sha256: 'abc',
    participants: [],
    action_items: [
      {
        id: 'mai_1', kind: 'action', description: 'Draft the rollout plan', owner_participant_id: null,
        due_date: '2026-10-31', line_start: 2, line_end: 3, status: 'pending', promoted_ticket_id: null,
        extracted_at: '2026-10-27T10:01:00Z',
        suggested_reminder: {
          title: 'Draft the rollout plan', due_date: '2026-10-31', due_at: '2026-10-31T09:00:00Z',
          lines: '2-3', owner_user_id: null,
        },
      },
      {
        id: 'mai_2', kind: 'action', description: 'Check the status page copy', owner_participant_id: null,
        due_date: null, line_start: 1, line_end: 1, status: 'pending', promoted_ticket_id: null,
        extracted_at: '2026-10-27T10:01:00Z',
      },
      {
        id: 'mai_3', kind: 'decision', description: 'Launch on November 10', owner_participant_id: null,
        due_date: '2026-11-10', line_start: 1, line_end: 2, status: 'pending', promoted_ticket_id: null,
        extracted_at: '2026-10-27T10:01:00Z',
        suggested_reminder: {
          title: 'Launch on November 10', due_date: '2026-11-10', due_at: '2026-11-10T09:00:00Z',
          lines: '1-2', owner_user_id: null,
        },
      },
    ],
    warnings: [],
  } as MeetingDetail;
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

const empty = { items: [], counts: { open: 0, done: 0, dismissed: 0 } };

beforeEach(() => {
  vi.clearAllMocks();
  client.getMeeting.mockResolvedValue(detail());
  client.listMeetingReminders.mockResolvedValue(empty);
  client.listAssignableUsers.mockResolvedValue({ users: [] });
  client.listPersonas.mockResolvedValue([]);
});

describe('meeting item reminders', () => {
  it('pre-fills from the suggestion, sends the viewer timezone and refreshes the strip', async () => {
    client.createMeetingItemReminder.mockResolvedValue({ id: 'rem_1' });
    wrap(view());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Add reminder: Draft the rollout plan' }));
    const dlg = await screen.findByRole('dialog', { name: 'Add reminder' });
    expect(await axe(dlg)).toHaveNoViolations();
    expect(screen.getByLabelText('Title')).toHaveValue('Draft the rollout plan');
    expect(screen.getByLabelText('Due')).toHaveValue('2026-10-31T09:00');
    const callsBefore = client.listMeetingReminders.mock.calls.length;
    await user.click(screen.getByRole('button', { name: 'Add reminder' }));
    await waitFor(() => expect(client.createMeetingItemReminder).toHaveBeenCalled());
    const [scope, mid, item, body] = client.createMeetingItemReminder.mock.calls[0];
    expect(scope).toEqual({ kind: 'project', id: 'proj_1' });
    expect(mid).toBe(MEETING_ID);
    expect(item).toBe('mai_1');
    expect(body).toEqual({
      title: 'Draft the rollout plan',
      due_at: new Date('2026-10-31T09:00').toISOString(),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(addToast).toHaveBeenCalledWith('success', 'Reminder added');
    await waitFor(() => expect(client.listMeetingReminders.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it('needs a date when the item has none, and is offered on decisions too', async () => {
    client.createMeetingItemReminder.mockResolvedValue({ id: 'rem_2' });
    wrap(view());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Add reminder: Check the status page copy' }));
    expect(screen.getByLabelText('Due')).toHaveValue('');
    await user.click(screen.getByRole('button', { name: 'Add reminder' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Pick a date and time.');
    expect(client.createMeetingItemReminder).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Add reminder: Launch on November 10' })).toBeInTheDocument();
  });

  it('shows a 422 from the server and keeps the dialog open', async () => {
    client.createMeetingItemReminder.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({ error: { code: 'reminder_owner_not_eligible', message: 'The owner cannot be reminded here.', details: {} } }),
      ),
    );
    wrap(view());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Add reminder: Draft the rollout plan' }));
    await user.click(screen.getByRole('button', { name: 'Add reminder' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The owner cannot be reminded here.');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(addToast).not.toHaveBeenCalled();
  });

  it('is hidden from viewers who cannot act', async () => {
    wrap(view(false));
    await screen.findByRole('heading', { name: 'Weekly platform sync' });
    expect(screen.queryByRole('button', { name: /Add reminder/ })).not.toBeInTheDocument();
  });
});
