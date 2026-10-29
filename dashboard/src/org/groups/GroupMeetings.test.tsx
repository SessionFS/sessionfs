import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MeetingDetail } from '../../api/client';
import GroupDetailPage from './GroupDetailPage';
import { ORG, authState, makeClient, renderWith, stubFetch, toast } from './groupTestHarness';

vi.mock('../../auth/AuthContext', async () => (await import('./groupTestHarness')).authModule);
vi.mock('../../hooks/useToast', async () => (await import('./groupTestHarness')).toastModule);

const PATH = '/settings/organization/groups/:groupId';
const ROUTE = `/settings/organization/groups/pg_1?org=${ORG}`;

const stored: MeetingDetail = {
  id: 'meet_00000000000000a1',
  org_id: ORG,
  project_id: null,
  group_id: 'pg_1',
  title: 'Program review',
  meeting_at: null,
  duration_minutes: null,
  source: 'manual_paste',
  line_count: 1,
  dlp_findings: 0,
  extraction_status: 'none',
  created_by_user_id: 'u_me',
  created_at: '2026-10-27T10:00:00Z',
  updated_at: '2026-10-27T10:00:00Z',
  deleted_at: null,
  content: 'Peer: I will draft the plan.',
  content_sha256: 'x',
  participants: [],
  action_items: [],
  warnings: ['duplicate_of: meet_00000000000000a0', 'extraction_skipped: llm_key_required'],
};

beforeEach(() => {
  authState.client = makeClient();
  toast.addToast.mockReset();
  stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

describe('Group meetings', () => {
  it('keeps the upload warnings when the new meeting opens', async () => {
    authState.client.uploadMeeting.mockResolvedValue(stored);
    authState.client.getMeeting.mockResolvedValue({ ...stored, warnings: [] });
    renderWith(<GroupDetailPage />, { route: ROUTE, path: PATH });
    const user = userEvent.setup();
    await screen.findByText('No meetings yet');
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a meeting' });
    await user.type(within(dialog).getByLabelText('Title'), 'Program review');
    fireEvent.change(within(dialog).getByLabelText('Transcript'), { target: { value: stored.content } });
    await user.click(within(dialog).getByRole('button', { name: 'Add meeting' }));

    expect(await screen.findByRole('heading', { name: 'Program review' })).toBeInTheDocument();
    expect(authState.client.uploadMeeting).toHaveBeenCalledWith({ kind: 'group', id: 'pg_1' }, expect.anything());
    expect(screen.getByText(/Saved without action items/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Set up an AI key' })).toHaveAttribute('href', '/settings/data-protection');
    expect(screen.getByRole('link', { name: 'open the earlier copy' })).toHaveAttribute(
      'href',
      '/meetings/meet_00000000000000a0',
    );
    // The open meeting leads the page in the DOM too (reading/tab order).
    const meeting = screen.getByRole('heading', { name: 'Program review' });
    const projects = screen.getByRole('heading', { name: 'Projects' });
    expect(meeting.compareDocumentPosition(projects) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('lists meetings after the projects when none is open', async () => {
    renderWith(<GroupDetailPage />, { route: ROUTE, path: PATH });
    const meetings = await screen.findByRole('heading', { name: 'Meetings' });
    const projects = screen.getByRole('heading', { name: 'Projects' });
    expect(projects.compareDocumentPosition(meetings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
