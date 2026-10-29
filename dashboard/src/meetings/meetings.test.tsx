import type { ReactNode } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type MeetingDetail, type MeetingPromotePreview, type MeetingSummary } from '../api/client';
import { axe } from '../test/axe';
import {
  describeFindings,
  extractionReason,
  meetingScopeHref,
  parseLines,
  parseWarnings,
  splitSpeaker,
  titleFromFileName,
  transcriptLines,
  uploadErrorMessage,
} from './format';
import MeetingsTab from './MeetingsTab';
import { UploadMeetingDialog } from './UploadMeetingDialog';
import { MeetingDetailView } from './MeetingDetailView';
import { PromoteDialog } from './PromoteDialog';

/**
 * Meetings (GitHub #116). The hooks run for real against a fake API client,
 * so these cover the request each control sends as well as what renders.
 */

const client = {
  listMeetings: vi.fn(),
  uploadMeeting: vi.fn(),
  getMeeting: vi.fn(),
  deleteMeeting: vi.fn(),
  extractMeeting: vi.fn(),
  resolveMeetingParticipant: vi.fn(),
  previewMeetingPromote: vi.fn(),
  promoteMeetingItem: vi.fn(),
  dismissMeetingItem: vi.fn(),
  restoreMeetingItem: vi.fn(),
  listAssignableUsers: vi.fn(),
  listPersonas: vi.fn(),
  listOrgPersonas: vi.fn(),
  getTicketById: vi.fn(),
  listJiraSprints: vi.fn(),
};
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));
vi.mock('../hooks/useMe', () => ({ useMe: () => ({ data: { user_id: 'u1', email: 'ola@example.test' } }) }));

const MEETING_ID = 'meet_0123456789abcdef';

function summary(overrides: Partial<MeetingSummary> = {}): MeetingSummary {
  return {
    id: MEETING_ID,
    org_id: 'org_1',
    project_id: 'proj_1',
    group_id: null,
    title: 'Weekly platform sync',
    meeting_at: '2026-10-27T00:00:00Z',
    duration_minutes: 30,
    source: 'manual_paste',
    line_count: 6,
    dlp_findings: 0,
    extraction_status: 'done',
    created_by_user_id: 'u1',
    created_at: '2026-10-27T10:00:00Z',
    updated_at: '2026-10-27T10:00:00Z',
    deleted_at: null,
    ...overrides,
  };
}

function detail(overrides: Partial<MeetingDetail> = {}): MeetingDetail {
  return {
    ...summary(),
    content: [
      'Priya: Let us lock the launch date today.',
      'Sam: Agreed, November 10.',
      'Priya: Sam, can you draft the rollout plan?',
      'Sam: I will have it by Friday.',
      'Kim: I will check the status page copy.',
      'Priya: Thanks all.',
    ].join('\n'),
    content_sha256: 'abc',
    participants: [
      { id: 'mp_1', name_raw: 'Priya', email_raw: null, user_id: 'u1', persona_name: null, resolution_method: 'display_name', resolved_by_user_id: null },
      { id: 'mp_2', name_raw: 'Sam', email_raw: null, user_id: 'u2', persona_name: null, resolution_method: 'display_name', resolved_by_user_id: null },
      { id: 'mp_3', name_raw: 'Kim', email_raw: null, user_id: null, persona_name: null, resolution_method: 'unresolved', resolved_by_user_id: null },
    ],
    action_items: [
      { id: 'mai_1', kind: 'action', description: 'Draft the rollout plan', owner_participant_id: 'mp_2', due_date: '2026-10-31', line_start: 3, line_end: 4, status: 'pending', promoted_ticket_id: null, extracted_at: '2026-10-27T10:01:00Z' },
      { id: 'mai_2', kind: 'action', description: 'Check the status page copy', owner_participant_id: 'mp_3', due_date: null, line_start: 5, line_end: 5, status: 'promoted', promoted_ticket_id: 'tk_00000000000000aa', extracted_at: '2026-10-27T10:01:00Z' },
      { id: 'mai_3', kind: 'decision', description: 'Launch on November 10', owner_participant_id: null, due_date: null, line_start: 1, line_end: 2, status: 'pending', promoted_ticket_id: null, extracted_at: '2026-10-27T10:01:00Z' },
    ],
    warnings: [],
    ...overrides,
  };
}

function wrap(node: ReactNode, path = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function apiError(status: number, detail: Record<string, unknown>): ApiError {
  const { code, message, ...rest } = detail as { code?: string; message?: string };
  return new ApiError(
    status,
    JSON.stringify({ error: { code: code ?? String(status), message: message ?? 'Error', details: rest } }),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  client.listAssignableUsers.mockResolvedValue({
    users: [
      { id: 'u1', display_name: 'Priya Shah', email: 'priya@example.test', can_edit: true },
      { id: 'u2', display_name: 'Sam Rivera', email: 'sam@example.test', can_edit: true },
      { id: 'u3', display_name: 'Kim Lee', email: 'kim@example.test', can_edit: true },
    ],
  });
  client.listPersonas.mockResolvedValue([]);
  client.getMeeting.mockResolvedValue(detail());
});

// ── Pure helpers ──

describe('meeting format helpers', () => {
  it('parses line ranges from the URL', () => {
    expect(parseLines('12-15')).toEqual({ start: 12, end: 15 });
    expect(parseLines('L7')).toEqual({ start: 7, end: 7 });
    expect(parseLines('9-3')).toEqual({ start: 3, end: 9 });
    expect(parseLines('0')).toBeNull();
    expect(parseLines('abc')).toBeNull();
    expect(parseLines(null)).toBeNull();
  });

  it('reads upload warnings', () => {
    expect(parseWarnings(['duplicate_of: meet_aaaa', 'extraction_skipped: llm_key_required'])).toEqual({
      duplicateOf: 'meet_aaaa',
      extractionSkipped: 'llm_key_required',
    });
    expect(parseWarnings([])).toEqual({ duplicateOf: null, extractionSkipped: null });
  });

  it('says what DLP found, in words', () => {
    expect(describeFindings({ aws_access_key: 2, email_address: 1 })).toBe('2 AWS access keys and 1 email address');
    expect(describeFindings({})).toBe('sensitive data');
  });

  it('turns upload errors into plain language', () => {
    expect(uploadErrorMessage(apiError(413, { error: 'meeting_too_large', message: 'Transcript is too large' }))).toMatch(
      /too large.*512 KB.*10,000 lines/,
    );
    expect(
      uploadErrorMessage(apiError(422, { error: 'dlp_blocked', message: 'DLP', findings: { aws_access_key: 1 } })),
    ).toMatch(/data protection policy blocked.*1 AWS access key\b/);
  });

  it('classifies extraction failures', () => {
    expect(extractionReason('llm_key_required')).toMatch(/No AI key/);
    expect(extractionReason('invalid_output')).toMatch(/could not be read/);
    expect(extractionReason('something_new')).toMatch(/failed/);
  });

  it('links a meeting to its project or group page', () => {
    expect(meetingScopeHref(summary(), '3-4')).toBe(`/projects/proj_1?tab=work&sub=meetings&meeting=${MEETING_ID}&lines=3-4`);
    expect(meetingScopeHref(summary({ project_id: null, group_id: 'pg_1' }))).toBe(
      `/settings/organization/groups/pg_1?org=org_1&meeting=${MEETING_ID}`,
    );
  });

  it('splits speakers, file titles and lines', () => {
    expect(splitSpeaker('Priya Shah: hello there')).toEqual(['Priya Shah', 'hello there']);
    expect(splitSpeaker('10:30 we started')).toBeNull();
    expect(titleFromFileName('weekly_sync 2026-10-27.vtt')).toBe('weekly sync 2026-10-27');
    expect(transcriptLines('a\nb\n')).toEqual(['a', 'b']);
  });
});

// ── List ──

function tab(props: Partial<Parameters<typeof MeetingsTab>[0]> = {}) {
  return (
    <MeetingsTab
      scope={{ kind: 'project', id: 'proj_1' }}
      orgId="org_1"
      canEdit
      canAdmin={false}
      meetingId={null}
      lines={null}
      onOpenMeeting={vi.fn()}
      onLinesChange={vi.fn()}
      {...props}
    />
  );
}

describe('MeetingsTab list', () => {
  it('lists meetings with their status and counts, and opens one', async () => {
    client.listMeetings.mockResolvedValue({
      meetings: [summary({ pending_action_count: 2, decision_count: 1 }), summary({ id: 'meet_ffffffffffffffff', title: 'Retro', extraction_status: 'failed' })],
      next_cursor: null,
    });
    const onOpen = vi.fn();
    wrap(tab({ onOpenMeeting: onOpen }));
    const row = await screen.findByRole('button', { name: /Weekly platform sync/ });
    expect(row).toHaveTextContent('6 lines');
    expect(row).toHaveTextContent('2 open actions');
    expect(row).toHaveTextContent('1 decision');
    expect(row).toHaveTextContent('Extracted');
    expect(screen.getByRole('button', { name: /Retro/ })).toHaveTextContent('Extraction failed');
    await userEvent.click(row);
    expect(onOpen).toHaveBeenCalledWith(MEETING_ID);
  });

  it('searches titles and transcripts', async () => {
    client.listMeetings.mockResolvedValue({ meetings: [summary()], next_cursor: null });
    wrap(tab());
    await screen.findByRole('button', { name: /Weekly platform sync/ });
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search meetings' }), 'launch');
    await waitFor(() =>
      expect(client.listMeetings).toHaveBeenLastCalledWith({ kind: 'project', id: 'proj_1' }, expect.objectContaining({ q: 'launch' })),
    );
  });

  it('explains meetings when there are none, with Add for editors only', async () => {
    client.listMeetings.mockResolvedValue({ meetings: [], next_cursor: null });
    const { unmount } = wrap(tab());
    expect(await screen.findByText('No meetings yet')).toBeInTheDocument();
    expect(screen.getByText(/pulls out the action items, decisions/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add meeting' })).toBeInTheDocument();
    unmount();
    wrap(tab({ canEdit: false }));
    expect(await screen.findByText('No meetings yet')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add meeting' })).not.toBeInTheDocument();
  });
});

// ── Upload ──

describe('UploadMeetingDialog', () => {
  it('saves a pasted transcript', async () => {
    client.uploadMeeting.mockResolvedValue(detail());
    const onUploaded = vi.fn();
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={onUploaded} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Title'), 'Standup');
    fireEvent.change(screen.getByLabelText('Transcript'), { target: { value: 'Priya: hi\nSam: hello' } });
    fireEvent.change(screen.getByLabelText('Meeting date (optional)'), { target: { value: '2026-10-27' } });
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
    expect(client.uploadMeeting).toHaveBeenCalledWith(
      { kind: 'project', id: 'proj_1' },
      {
        title: 'Standup',
        content: 'Priya: hi\nSam: hello',
        source: 'manual_paste',
        meeting_at: '2026-10-27T00:00:00Z',
        extract: true,
      },
    );
  });

  it('reads a picked file client-side, prefills the title and sends source=file_upload', async () => {
    client.uploadMeeting.mockResolvedValue(detail());
    wrap(<UploadMeetingDialog scope={{ kind: 'group', id: 'pg_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    const file = new File(['WEBVTT\n\n00:00.000 --> 00:01.000\n<v Priya>Hello'], 'Q4 planning.vtt', { type: 'text/vtt' });
    await user.upload(screen.getByLabelText('Transcript file'), file);
    expect(await screen.findByText('Q4 planning.vtt')).toBeInTheDocument();
    expect(screen.getByLabelText('Title')).toHaveValue('Q4 planning');
    await user.click(screen.getByLabelText(/Extract action items now/));
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    await waitFor(() => expect(client.uploadMeeting).toHaveBeenCalled());
    expect(client.uploadMeeting.mock.calls[0][1]).toMatchObject({
      title: 'Q4 planning',
      source: 'file_upload',
      extract: false,
      meeting_at: null,
    });
    expect(client.uploadMeeting.mock.calls[0][1].content).toContain('<v Priya>Hello');
  });

  it('shows 413 and DLP refusals in plain language', async () => {
    client.uploadMeeting.mockRejectedValueOnce(apiError(413, { error: 'meeting_too_large', message: 'Transcript is too large' }));
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Title'), 'Big');
    fireEvent.change(screen.getByLabelText('Transcript'), { target: { value: 'text' } });
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    expect(await screen.findByText(/This transcript is too large/)).toBeInTheDocument();

    client.uploadMeeting.mockRejectedValueOnce(
      apiError(422, { error: 'dlp_blocked', message: 'DLP policy blocked upload', findings: { email_address: 3 } }),
    );
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    expect(await screen.findByText(/contains 3 email addresses/)).toBeInTheDocument();
  });

  it('refuses a file over 1 MB without reading it', async () => {
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const big = new File(['x'], 'huge.txt');
    Object.defineProperty(big, 'size', { value: 2 * 1024 * 1024 });
    const read = vi.spyOn(big, 'text');
    fireEvent.change(screen.getByLabelText('Transcript file'), { target: { files: [big] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('larger than 1 MB');
    expect(read).not.toHaveBeenCalled();
  });

  it('holds caption files to the 1 MB body cap, plain text to 512 KB', async () => {
    const body = 'a'.repeat(600 * 1024);
    const { unmount } = wrap(
      <UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('Transcript file'), { target: { files: [new File([body], 'call.vtt')] } });
    expect(await screen.findByText('call.vtt')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    unmount();
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('Transcript file'), { target: { files: [new File([body], 'call.txt')] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('larger than 512 KB');
  });

  it('measures caption files as the encoded request body', async () => {
    // ~990 KB of short lines: under 1 MiB raw, over it once every newline
    // is escaped in the JSON body.
    const body = 'a\n'.repeat(495 * 1024);
    const onUploaded = vi.fn();
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={onUploaded} />);
    fireEvent.change(screen.getByLabelText('Transcript file'), { target: { files: [new File([body], 'long.vtt')] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('over the 1 MB upload size limit');
    expect(client.uploadMeeting).not.toHaveBeenCalled();
  });

  it('explains the post-timestamp limits when the server refuses a caption file', async () => {
    client.uploadMeeting.mockRejectedValue(apiError(413, { error: 'meeting_too_large', message: 'Transcript is too large' }));
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    fireEvent.change(screen.getByLabelText('Transcript file'), {
      target: { files: [new File(['WEBVTT\n\n00:00.000 --> 00:01.000\nhi'], 'call.vtt')] },
    });
    await screen.findByText('call.vtt');
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    const alert = await screen.findByText(/After timestamps are removed.*512 KB or 10,000 lines/);
    expect(alert).toBeInTheDocument();
    expect(screen.queryByText(/1 MB upload size limit/)).not.toBeInTheDocument();
  });

  it('refuses unsupported files before reading them', async () => {
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const input = screen.getByLabelText('Transcript file');
    fireEvent.change(input, { target: { files: [new File(['x'], 'notes.pdf')] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a .txt, .md, .vtt, .srt file.');
  });
});

// ── Detail ──

function detailView(props: Partial<Parameters<typeof MeetingDetailView>[0]> = {}) {
  return (
    <MeetingDetailView
      meetingId={MEETING_ID}
      scope={{ kind: 'project', id: 'proj_1' }}
      orgId="org_1"
      canEdit
      canAdmin={false}
      lines={null}
      onLinesChange={vi.fn()}
      onBack={vi.fn()}
      {...props}
    />
  );
}

describe('MeetingDetailView', () => {
  it('shows the numbered transcript, items, decisions and participants', async () => {
    const { container } = wrap(detailView());
    expect(await screen.findByRole('heading', { name: 'Weekly platform sync' })).toBeInTheDocument();
    const transcript = screen.getByRole('list', { name: 'Transcript' });
    expect(within(transcript).getAllByRole('listitem')).toHaveLength(6);
    expect(screen.getByRole('button', { name: /^Draft the rollout plan/ })).toHaveTextContent('Sam Rivera');
    expect(screen.getByRole('button', { name: /^Draft the rollout plan/ })).toHaveTextContent('Lines 3–4');
    expect(screen.getByRole('region', { name: /Decisions/ })).toHaveTextContent('Launch on November 10');
    expect(screen.getByRole('link', { name: 'tk_00000000000000aa' })).toHaveAttribute(
      'href',
      '/projects/proj_1?tab=work&sub=meetings'.replace('meetings', 'tickets') + '&ticket=tk_00000000000000aa',
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('highlights the cited lines when an item is clicked', async () => {
    const onLinesChange = vi.fn();
    const { rerender } = wrap(detailView({ onLinesChange }));
    const item = await screen.findByRole('button', { name: /^Draft the rollout plan/ });
    await userEvent.click(item);
    expect(item).toHaveAttribute('aria-pressed', 'true');
    expect(onLinesChange).toHaveBeenCalledWith('3-4');
    // The URL drives the highlight: re-render as the page would.
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>{detailView({ onLinesChange, lines: '3-4' })}</MemoryRouter>
      </QueryClientProvider>,
    );
    const transcript = await screen.findByRole('list', { name: 'Transcript' });
    const highlighted = transcript.querySelectorAll('[data-highlighted]');
    expect([...highlighted].map((el) => el.getAttribute('data-line'))).toEqual(['3', '4']);
  });

  it('highlights lines from the URL (a helper citation)', async () => {
    wrap(detailView({ lines: '5' }));
    const transcript = await screen.findByRole('list', { name: 'Transcript' });
    expect(transcript.querySelector('[data-line="5"]')).toHaveAttribute('data-highlighted', 'true');
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument();
  });

  it('shows a friendly not-found for a deleted or unknown meeting', async () => {
    client.getMeeting.mockRejectedValue(apiError(404, { message: 'Meeting not found' }));
    wrap(detailView());
    expect(await screen.findByText('Meeting not found')).toBeInTheDocument();
    expect(screen.getByText(/deleted, or you don’t have access/)).toBeInTheDocument();
  });

  it('is read-only for readers', async () => {
    wrap(detailView({ canEdit: false }));
    await screen.findByRole('heading', { name: 'Weekly platform sync' });
    expect(screen.queryByRole('button', { name: /Create ticket/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Dismiss/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Re-extract|Extract action items/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Match/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('offers Delete to the uploader and to admins only', async () => {
    // The caller (u1) uploaded it.
    const first = wrap(detailView());
    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument();
    first.unmount();
    // Someone else's meeting: an editor can't delete it…
    client.getMeeting.mockResolvedValue(detail({ created_by_user_id: 'u9' }));
    const second = wrap(detailView());
    await screen.findByRole('heading', { name: 'Weekly platform sync' });
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    second.unmount();
    // …but a project or group admin can.
    wrap(detailView({ canAdmin: true }));
    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('explains a failed extraction in words', async () => {
    client.extractMeeting.mockResolvedValue({ ...detail({ extraction_status: 'failed', extraction_error: 'timeout' }), dropped_items: 0 });
    wrap(detailView());
    await userEvent.click(await screen.findByRole('button', { name: 'Re-extract' }));
    expect(await screen.findByText('The model took too long to answer.')).toBeInTheDocument();
  });

  it('links to AI key settings when no key is set up', async () => {
    client.extractMeeting.mockRejectedValue(apiError(409, { code: 'llm_key_required', message: 'No LLM key' }));
    wrap(detailView());
    await userEvent.click(await screen.findByRole('button', { name: 'Re-extract' }));
    expect(await screen.findByText(/No AI key is set up/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Set up an AI key' })).toHaveAttribute('href', '/settings/data-protection');
  });

  it('dismisses and restores an item, re-rendering it each time', async () => {
    const base = detail();
    const dismissedItem = { ...base.action_items[0], status: 'dismissed' as const };
    const withItem = (item: typeof dismissedItem | (typeof base.action_items)[0]) => ({
      ...base,
      action_items: base.action_items.map((i) => (i.id === item.id ? item : i)),
    });
    client.dismissMeetingItem.mockImplementation(async () => {
      client.getMeeting.mockResolvedValue(withItem(dismissedItem));
      return dismissedItem;
    });
    client.restoreMeetingItem.mockImplementation(async () => {
      client.getMeeting.mockResolvedValue(base);
      return base.action_items[0];
    });
    wrap(detailView());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Dismiss: Draft the rollout plan' }));
    await waitFor(() => expect(client.dismissMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_1'));
    const restore = await screen.findByRole('button', { name: 'Restore: Draft the rollout plan' });
    expect(screen.queryByRole('button', { name: 'Create ticket: Draft the rollout plan' })).not.toBeInTheDocument();
    await user.click(restore);
    await waitFor(() => expect(client.restoreMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_1'));
    expect(await screen.findByRole('button', { name: 'Create ticket: Draft the rollout plan' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore: Draft the rollout plan' })).not.toBeInTheDocument();
  });

  it('creates a ticket from an item, which then shows the ticket', async () => {
    const base = detail();
    client.previewMeetingPromote.mockResolvedValue(preview());
    client.promoteMeetingItem.mockImplementation(async () => {
      client.getMeeting.mockResolvedValue({
        ...base,
        action_items: base.action_items.map((i) =>
          i.id === 'mai_1' ? { ...i, status: 'promoted', promoted_ticket_id: 'tk_00000000000000bb' } : i,
        ),
      });
      return { ticket_id: 'tk_00000000000000bb', item_id: 'mai_1', status: 'promoted', published_to_jira: false, warnings: [] };
    });
    wrap(detailView());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create ticket: Draft the rollout plan' }));
    await screen.findByLabelText('Title');
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    const link = await screen.findByRole('link', { name: 'tk_00000000000000bb' });
    expect(link).toHaveAttribute('href', '/projects/proj_1?tab=work&sub=tickets&ticket=tk_00000000000000bb');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create ticket: Draft the rollout plan' })).not.toBeInTheDocument();
  });

  it('keeps the dialog open and explains a failed promote', async () => {
    client.previewMeetingPromote.mockResolvedValue(preview());
    client.promoteMeetingItem.mockRejectedValue(apiError(409, { code: 'item_not_pending', message: 'no longer pending' }));
    wrap(detailView());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create ticket: Draft the rollout plan' }));
    await screen.findByLabelText('Title');
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    expect(await screen.findByText(/already handled/)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(addToast).not.toHaveBeenCalledWith('success', expect.anything());
  });

  it('explains an extract refused for edit rights instead of blaming the model', async () => {
    client.extractMeeting.mockRejectedValue(
      apiError(403, { code: 'project_edit_forbidden', message: 'You cannot edit this project' }),
    );
    wrap(detailView());
    await userEvent.click(await screen.findByRole('button', { name: 'Re-extract' }));
    expect(await screen.findByText('You can view this meeting but not change it.')).toBeInTheDocument();
    expect(screen.queryByText('The model request failed.')).not.toBeInTheDocument();
  });
});

// ── Participant picker ──

describe('participant matching', () => {
  it('matches an unresolved participant to a person', async () => {
    client.resolveMeetingParticipant.mockResolvedValue({
      ...detail().participants[2],
      user_id: 'u3',
      resolution_method: 'manual',
    });
    wrap(detailView());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Match Kim' }));
    await user.click(screen.getByRole('button', { name: /Match “Kim” to/ }));
    await user.click(await screen.findByRole('option', { name: /Kim Lee/ }));
    await waitFor(() =>
      expect(client.resolveMeetingParticipant).toHaveBeenCalledWith(MEETING_ID, 'mp_3', { user_id: 'u3' }),
    );
    expect(addToast).toHaveBeenCalledWith('success', 'Matched Kim');
  });

  it('shows why a match was refused', async () => {
    client.resolveMeetingParticipant.mockRejectedValue(
      apiError(422, { error: 'participant_not_eligible', message: 'Participant must be…' }),
    );
    wrap(detailView());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Match Kim' }));
    await user.click(screen.getByRole('button', { name: /Match “Kim” to/ }));
    await user.click(await screen.findByRole('option', { name: /Kim Lee/ }));
    expect(await screen.findByText(/can’t be matched in this meeting’s scope/)).toBeInTheDocument();
  });
});

// ── Promote ──

function preview(overrides: Partial<MeetingPromotePreview> = {}): MeetingPromotePreview {
  return {
    meeting_id: MEETING_ID,
    item_id: 'mai_1',
    title: 'Draft the rollout plan',
    description: 'Draft the rollout plan\n\nFrom meeting *Weekly platform sync* (2026-10-27), lines 3-4:\n> Priya: Sam, can you draft the rollout plan?\n> Sam: I will have it by Friday.',
    priority: 'medium',
    kind: 'task',
    assigned_to: null,
    assigned_user_id: 'u2',
    due_date: '2026-10-31',
    context_refs: [`meeting:${MEETING_ID}#mai_1`],
    target_project_id: 'proj_1',
    target_project_options: null,
    ...overrides,
  };
}

describe('PromoteDialog', () => {
  const item = detail().action_items[0];

  it('shows the editable draft and creates the ticket on confirm', async () => {
    client.previewMeetingPromote.mockResolvedValue(preview());
    client.promoteMeetingItem.mockResolvedValue({ ticket_id: 'tk_00000000000000bb', item_id: 'mai_1', status: 'promoted', published_to_jira: false, warnings: [] });
    const onClose = vi.fn();
    const { container } = wrap(<PromoteDialog meetingId={MEETING_ID} item={item} onClose={onClose} />);
    const title = await screen.findByLabelText('Title');
    expect(title).toHaveValue('Draft the rollout plan');
    expect(screen.getByLabelText('Description')).toHaveValue(preview().description);
    expect(screen.getByLabelText('Due date (optional)')).toHaveValue('2026-10-31');
    expect(await screen.findByRole('button', { name: 'Assignee' })).toHaveTextContent('Sam Rivera');
    expect(screen.queryByLabelText(/Also publish to Jira/)).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    const user = userEvent.setup();
    await user.clear(title);
    await user.type(title, 'Write the rollout plan');
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(client.promoteMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_1', {
      title: 'Write the rollout plan',
      description: preview().description,
      priority: 'medium',
      kind: 'task',
      assigned_user_id: 'u2',
      assigned_to: null,
      due_date: '2026-10-31',
    });
    expect(addToast).toHaveBeenCalledWith('success', 'Created ticket tk_00000000000000bb');
  });

  it('requires a target project for a group meeting and asks the preview about Jira for it', async () => {
    const groupDraft = preview({
      target_project_id: null,
      assigned_user_id: null,
      jira_publish_available: false,
      target_project_options: [
        { id: 'proj_1', name: 'Widgets' },
        { id: 'proj_2', name: 'Gadgets' },
      ],
    });
    // The server judges Jira for the target named in ?target_project_id.
    client.previewMeetingPromote.mockImplementation(async (_m: string, _i: string, target?: string) => ({
      ...groupDraft,
      jira_publish_available: target === 'proj_2',
    }));
    client.promoteMeetingItem.mockResolvedValue({ ticket_id: 'tk_00000000000000cc', item_id: 'mai_1', status: 'promoted', published_to_jira: true, warnings: [] });
    wrap(<PromoteDialog meetingId={MEETING_ID} item={item} onClose={vi.fn()} />);
    const user = userEvent.setup();
    await screen.findByLabelText('Title');
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    expect(await screen.findByText('Choose the project to create the ticket in.')).toBeInTheDocument();
    expect(client.promoteMeetingItem).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Project' }));
    await user.click(await screen.findByRole('option', { name: 'Widgets' }));
    await waitFor(() => expect(client.previewMeetingPromote).toHaveBeenCalledWith(MEETING_ID, 'mai_1', 'proj_1'));
    expect(screen.queryByLabelText(/Also publish to Jira/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Project' }));
    await user.click(await screen.findByRole('option', { name: 'Gadgets' }));
    await user.click(await screen.findByLabelText(/Also publish to Jira/));
    expect(client.previewMeetingPromote).toHaveBeenCalledWith(MEETING_ID, 'mai_1', 'proj_2');
    // The edited draft is kept when the target changes.
    expect(screen.getByLabelText('Title')).toHaveValue(groupDraft.title);
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    await waitFor(() => expect(client.promoteMeetingItem).toHaveBeenCalled());
    expect(client.promoteMeetingItem.mock.calls[0][2]).toMatchObject({ target_project_id: 'proj_2', publish_to_jira: true });
    expect(client.listJiraSprints).not.toHaveBeenCalled();
  });

  it('offers Jira from the preview flag, never by calling Jira', async () => {
    client.previewMeetingPromote.mockResolvedValue(preview({ jira_publish_available: true }));
    wrap(<PromoteDialog meetingId={MEETING_ID} item={item} onClose={vi.fn()} />);
    expect(await screen.findByLabelText(/Also publish to Jira/)).toBeInTheDocument();
    expect(client.listJiraSprints).not.toHaveBeenCalled();
  });

  it('hides the Jira option when the preview says publishing is not available', async () => {
    client.previewMeetingPromote.mockResolvedValue(preview({ jira_publish_available: false }));
    wrap(<PromoteDialog meetingId={MEETING_ID} item={item} onClose={vi.fn()} />);
    await screen.findByLabelText('Title');
    expect(screen.queryByLabelText(/Also publish to Jira/)).not.toBeInTheDocument();
    expect(client.listJiraSprints).not.toHaveBeenCalled();
  });

  it('explains a Jira preflight refusal in words', async () => {
    client.previewMeetingPromote.mockResolvedValue(preview({ jira_publish_available: true }));
    client.promoteMeetingItem.mockRejectedValue(
      apiError(409, { error: 'jira_not_connected', message: 'This organization has not connected Jira.' }),
    );
    wrap(<PromoteDialog meetingId={MEETING_ID} item={item} onClose={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText(/Also publish to Jira/));
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    expect(await screen.findByText(/hasn’t connected Jira, so nothing was created/)).toBeInTheDocument();
  });

  it('explains an item that was already promoted', async () => {
    client.previewMeetingPromote.mockRejectedValue(apiError(409, { code: 'item_not_pending', message: 'no longer pending' }));
    wrap(<PromoteDialog meetingId={MEETING_ID} item={item} onClose={vi.fn()} />);
    expect(await screen.findByText(/already handled/)).toBeInTheDocument();
  });
});

// Keep `act` referenced for environments that warn without it.
void act;

// ── Codex round 1: scope, in-flight writes, concurrent item writes ──

function Where() {
  const loc = useLocation();
  return <p data-testid="where">{`${loc.pathname}${loc.search}`}</p>;
}

const never = () => new Promise<never>(() => {});

describe('meeting from another scope', () => {
  it('opens it on its own page instead of with this page’s permissions', async () => {
    client.getMeeting.mockResolvedValue(detail({ project_id: 'proj_other' }));
    wrap(
      <>
        {detailView({ lines: '3-4', canAdmin: true })}
        <Where />
      </>,
    );
    await waitFor(() =>
      expect(screen.getByTestId('where')).toHaveTextContent(
        `/projects/proj_other?tab=work&sub=meetings&meeting=${MEETING_ID}&lines=3-4`,
      ),
    );
    expect(screen.queryByRole('button', { name: /Re-extract|Create ticket|Dismiss|Delete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Weekly platform sync' })).not.toBeInTheDocument();
  });

  it('does the same for a project meeting opened on a group page', async () => {
    wrap(
      <>
        {detailView({ scope: { kind: 'group', id: 'pg_1' } })}
        <Where />
      </>,
    );
    await waitFor(() =>
      expect(screen.getByTestId('where')).toHaveTextContent(`/projects/proj_1?tab=work&sub=meetings&meeting=${MEETING_ID}`),
    );
    expect(screen.queryByRole('button', { name: 'Re-extract' })).not.toBeInTheDocument();
  });
});

describe('writes in flight', () => {
  it('keeps the upload dialog open while it saves', async () => {
    client.uploadMeeting.mockImplementation(never);
    const onClose = vi.fn();
    wrap(<UploadMeetingDialog scope={{ kind: 'project', id: 'proj_1' }} onClose={onClose} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Title'), 'Standup');
    fireEvent.change(screen.getByLabelText('Transcript'), { target: { value: 'Priya: hi' } });
    await user.click(screen.getByRole('button', { name: 'Add meeting' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled());
    await user.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Add a meeting' })).toBeInTheDocument();
  });

  it('keeps the promote dialog open while the ticket is created', async () => {
    client.previewMeetingPromote.mockResolvedValue(preview());
    client.promoteMeetingItem.mockImplementation(never);
    const onClose = vi.fn();
    wrap(<PromoteDialog meetingId={MEETING_ID} item={detail().action_items[0]} onClose={onClose} />);
    const user = userEvent.setup();
    await screen.findByLabelText('Title');
    await user.click(screen.getByRole('button', { name: 'Create ticket' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled());
    await user.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps the delete confirmation open while the meeting is deleted', async () => {
    client.deleteMeeting.mockImplementation(never);
    wrap(detailView());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete this meeting?' });
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled());
    await user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: 'Delete this meeting?' })).toBeInTheDocument();
  });

  it('keeps every item with a write in flight disabled, not just the last one', async () => {
    const base = detail();
    client.getMeeting.mockResolvedValue({
      ...base,
      action_items: base.action_items.map((i) => (i.id === 'mai_2' ? { ...i, status: 'pending', promoted_ticket_id: null } : i)),
    });
    // Each dismiss stays in flight until the test answers it.
    const answers: Array<(v: unknown) => void> = [];
    client.dismissMeetingItem.mockImplementation(() => new Promise((r) => answers.push(r)));
    wrap(detailView());
    const user = userEvent.setup();
    const dismissA = await screen.findByRole('button', { name: 'Dismiss: Draft the rollout plan' });
    const dismissB = screen.getByRole('button', { name: 'Dismiss: Check the status page copy' });
    await user.click(dismissA);
    await waitFor(() => expect(dismissA).toBeDisabled());
    await user.click(dismissB);
    await waitFor(() => expect(dismissB).toBeDisabled());
    expect(dismissA).toBeDisabled();

    // Both settle: both recover, the first one too.
    await act(async () => {
      answers[0]({ ...base.action_items[0], status: 'pending' });
      answers[1]({ ...base.action_items[1], status: 'pending', promoted_ticket_id: null });
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Dismiss: Draft the rollout plan' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Dismiss: Check the status page copy' })).toBeEnabled();
  });

  it('dismisses a pending decision, which never offers a ticket', async () => {
    client.dismissMeetingItem.mockResolvedValue({ ...detail().action_items[2], status: 'dismissed' });
    wrap(detailView());
    const decisions = await screen.findByRole('region', { name: /Decisions/ });
    expect(within(decisions).queryByRole('button', { name: /Create ticket/ })).not.toBeInTheDocument();
    await userEvent.click(within(decisions).getByRole('button', { name: 'Dismiss: Launch on November 10' }));
    await waitFor(() => expect(client.dismissMeetingItem).toHaveBeenCalledWith(MEETING_ID, 'mai_3'));
  });

  it('keeps Tab inside the delete confirmation while every button is disabled', async () => {
    client.deleteMeeting.mockImplementation(never);
    wrap(
      <>
        {detailView()}
        <button type="button">Outside</button>
      </>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete this meeting?' });
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled());
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    // Focus dropped onto <body> (a browser blurs a button when it is disabled).
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.getByRole('button', { name: 'Outside' })).not.toHaveFocus();
  });
});
