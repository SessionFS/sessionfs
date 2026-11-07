import type { ReactNode } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type MeetingDetail, type MeetingSummary } from '../api/client';
import { axe } from '../test/axe';
import {
  chatMessages,
  chatTooLarge,
  chatUploadErrorMessage,
  MAX_CHAT_BYTES,
  discordLacksGuildChannel,
  needsAttestation,
  slackLacksChannelObject,
} from './chat';
import { sourceHref } from '../reminders/format';
import { PromoteDialog } from './PromoteDialog';
import { ChatMessages } from './ChatMessages';
import { MeetingDetailView } from './MeetingDetailView';
import MeetingsTab from './MeetingsTab';
import { UploadChatDialog } from './UploadChatDialog';

/** Chat threads in the Conversations surface (GitHub #118), real hooks over a fake client. */

const client = {
  listMeetings: vi.fn(),
  uploadChatThread: vi.fn(),
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
  listMeetingReminders: vi.fn(),
};

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { client, apiKey: 'k', baseUrl: '' } }),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('../hooks/useMe', () => ({ useMe: () => ({ data: { user_id: 'u1', email: 'ola@example.test' } }) }));

const CHAT_ID = 'chat_0123456789abcdef';
const SLACK_WITH_CHANNEL = '{"channel":{"id":"C1","name":"infra"},"messages":[{"text":"hi"}]}';
const SCOPE = { kind: 'project', id: 'proj_1' } as const;

function chatSummary(o: Partial<MeetingSummary> = {}): MeetingSummary {
  return {
    id: CHAT_ID,
    org_id: 'org_1',
    project_id: 'proj_1',
    group_id: null,
    kind: 'chat',
    provider: 'slack',
    channel_name: '#infra',
    title: 'LB cert decision',
    meeting_at: null,
    duration_minutes: null,
    source: 'chat_export',
    line_count: 3,
    dlp_findings: 0,
    extraction_status: 'done',
    created_by_user_id: 'u1',
    created_at: '2026-10-29T14:10:00Z',
    updated_at: '2026-10-29T14:10:00Z',
    deleted_at: null,
    ...o,
  };
}

function chatDetail(o: Partial<MeetingDetail> = {}): MeetingDetail {
  return {
    ...chatSummary(),
    content: [
      '[2026-10-29 14:02] Tolu: We are going with Dataproc.',
      '[2026-10-29 14:03] Sam: I will own the ticket. ⏎ <b>bold</b> stays text',
      '[2026-10-29 14:05] Tolu: Thanks',
    ].join('\n'),
    content_sha256: 'abc',
    messages: [
      { line_no: 1, author_raw: 'Tolu', posted_at: '2026-10-29T14:02:00Z' },
      { line_no: 2, author_raw: 'Sam', posted_at: '2026-10-29T14:03:00Z' },
      { line_no: 3, author_raw: 'Tolu', posted_at: '2026-10-29T14:05:00Z' },
    ],
    participants: [
      { id: 'mp_1', name_raw: 'Tolu', email_raw: null, user_id: null, persona_name: null, resolution_method: 'unresolved', resolved_by_user_id: null },
    ],
    action_items: [
      { id: 'mai_1', kind: 'action', description: 'Own the Dataproc ticket', owner_participant_id: null, due_date: null, line_start: 2, line_end: 2, status: 'pending', promoted_ticket_id: null, extracted_at: '2026-10-29T14:11:00Z' },
    ],
    warnings: [],
    ...o,
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

function apiError(status: number, detail: Record<string, unknown>): ApiError {
  const { code, message, ...rest } = detail as { code?: string; message?: string };
  return new ApiError(status, JSON.stringify({ error: { code: code ?? String(status), message: message ?? 'Error', details: rest } }));
}

beforeEach(() => {
  vi.clearAllMocks();
  client.listAssignableUsers.mockResolvedValue({ users: [] });
  client.listPersonas.mockResolvedValue([]);
  client.listOrgPersonas.mockResolvedValue([]);
  client.listMeetingReminders.mockResolvedValue({ items: [], counts: { open: 0, done: 0, dismissed: 0 } });
  client.getMeeting.mockResolvedValue(chatDetail());
});

describe('chat helpers', () => {
  it('splits author and time from the side table and unfolds line breaks', () => {
    const msgs = chatMessages(chatDetail());
    expect(msgs.map((m) => [m.n, m.author, m.text])).toEqual([
      [1, 'Tolu', 'We are going with Dataproc.'],
      [2, 'Sam', 'I will own the ticket.\n<b>bold</b> stays text'],
      [3, 'Tolu', 'Thanks'],
    ]);
  });

  it('keeps the stored line when there is no message meta', () => {
    expect(chatMessages({ content: '[t] A: hi', messages: undefined })[0]).toMatchObject({ author: null, text: '[t] A: hi' });
  });

  it('enforces the 2 MiB limit', () => {
    expect(chatTooLarge('x'.repeat(MAX_CHAT_BYTES))).toBeNull();
    expect(chatTooLarge('x'.repeat(MAX_CHAT_BYTES + 1))).toMatch(/larger than 2 MB/);
  });

  it('needs an attestation for a paste and a Slack export with no channel object', () => {
    expect(slackLacksChannelObject('[]')).toBe(true);
    expect(slackLacksChannelObject('{"messages": []}')).toBe(true);
    expect(slackLacksChannelObject('{"channel": "C1", "messages": []}')).toBe(true);
    expect(slackLacksChannelObject('{"channel": {"id": "C1"}, "messages": []}')).toBe(false);
    expect(slackLacksChannelObject('{"channel_info": {"id": "C1"}, "messages": []}')).toBe(false);
    expect(slackLacksChannelObject('not json')).toBe(false);
    expect(needsAttestation('paste')).toBe(true);
    expect(needsAttestation('slack', '[]')).toBe(true);
    expect(needsAttestation('slack', '')).toBe(false);
    expect(needsAttestation('slack', SLACK_WITH_CHANNEL)).toBe(false);
    expect(needsAttestation('discord', '[]')).toBe(false);
  });

  it('needs an attestation for a Discord export without guild channel details', () => {
    const msgs = [{ content: 'hi', author: { name: 'a' } }];
    const guild = { id: '1', name: 'Acme' };
    expect(discordLacksGuildChannel(JSON.stringify({ messages: msgs }))).toBe(true);
    expect(discordLacksGuildChannel(JSON.stringify({ channel: { type: 'GuildTextChat' }, messages: msgs }))).toBe(true);
    expect(discordLacksGuildChannel(JSON.stringify({ guild, channel: { type: 'Mystery' }, messages: msgs }))).toBe(true);
    expect(discordLacksGuildChannel(JSON.stringify({ guild, channel: { name: 'x' }, messages: msgs }))).toBe(true);
    for (const type of ['GuildTextChat', 'GuildPublicThread', 0, 11, '12']) {
      expect(discordLacksGuildChannel(JSON.stringify({ guild, channel: { type }, messages: msgs }))).toBe(false);
    }
    expect(discordLacksGuildChannel('not json')).toBe(false);
    expect(needsAttestation('discord', JSON.stringify({ messages: msgs }))).toBe(true);
  });

  it('links a chat_item reminder source to its thread', () => {
    expect(
      sourceHref({ kind: 'chat_item', id: 'mai_1', lines: '2-2', title: null, project_id: 'proj_1', group_id: null, meeting_id: CHAT_ID }),
    ).toBe(`/meetings/${CHAT_ID}?lines=2-2`);
  });

  it('puts server errors into words', () => {
    expect(chatUploadErrorMessage(apiError(422, { code: 'dm_not_supported' }))).toMatch(/Direct-message/);
    expect(chatUploadErrorMessage(apiError(422, { code: 'attestation_required' }))).toMatch(/not a direct message/);
    expect(chatUploadErrorMessage(apiError(422, { code: 'dlp_blocked', findings: { aws_access_key: 2 } }))).toMatch(/2 AWS access keys/);
  });
});

describe('ChatMessages', () => {
  it('numbers messages M1.. and renders content as plain text', async () => {
    const { container } = wrap(<ChatMessages thread={chatDetail()} highlight={{ start: 2, end: 2 }} />);
    const list = screen.getByRole('list', { name: 'Messages' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    expect(list).toHaveTextContent('M1');
    expect(list).toHaveTextContent('<b>bold</b> stays text');
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('[data-highlighted]')).toHaveTextContent('Sam');
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Conversations list', () => {
  function tab() {
    return (
      <MeetingsTab
        scope={SCOPE}
        orgId="org_1"
        canEdit
        canAdmin={false}
        meetingId={null}
        lines={null}
        onOpenMeeting={vi.fn()}
        onLinesChange={vi.fn()}
      />
    );
  }

  it('lists both kinds, shows provider, channel and message count on chat rows, and filters by kind', async () => {
    client.listMeetings.mockResolvedValue({ meetings: [chatSummary()], next_cursor: null });
    const { container } = wrap(tab());
    const row = await screen.findByRole('button', { name: /LB cert decision/ });
    expect(row).toHaveTextContent('Slack');
    expect(row).toHaveTextContent('#infra');
    expect(row).toHaveTextContent('3 messages');
    expect(client.listMeetings).toHaveBeenLastCalledWith(SCOPE, expect.objectContaining({ kind: 'all' }));
    await userEvent.click(screen.getByRole('tab', { name: 'Chat' }));
    await waitFor(() => expect(client.listMeetings).toHaveBeenLastCalledWith(SCOPE, expect.objectContaining({ kind: 'chat' })));
    await userEvent.click(screen.getByRole('tab', { name: 'Meetings' }));
    await waitFor(() => expect(client.listMeetings).toHaveBeenLastCalledWith(SCOPE, expect.objectContaining({ kind: 'meeting' })));
    expect(await axe(container)).toHaveNoViolations();
  });

  it('opens the chat upload dialog from Add chat thread', async () => {
    client.listMeetings.mockResolvedValue({ meetings: [chatSummary()], next_cursor: null });
    wrap(tab());
    await screen.findByRole('button', { name: /LB cert decision/ });
    await userEvent.click(screen.getByRole('button', { name: 'Add chat thread' }));
    expect(await screen.findByRole('dialog', { name: 'Add a chat thread' })).toBeInTheDocument();
  });
});

describe('Chat thread view', () => {
  it('shows messages, items with message ranges and participants, and reminder controls', async () => {
    const { container } = wrap(
      <MeetingDetailView
        meetingId={CHAT_ID}
        scope={SCOPE}
        orgId="org_1"
        canEdit
        canAdmin={false}
        lines="2"
        onLinesChange={vi.fn()}
        onBack={vi.fn()}
      />,
    );
    expect(await screen.findByRole('heading', { name: 'LB cert decision' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Messages' })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Messages' })).toHaveTextContent('We are going with Dataproc.');
    expect(screen.getAllByText('Message 2').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('button', { name: /Add reminder: Own the Dataproc ticket/ })).toBeInTheDocument();
    await waitFor(() => expect(client.listMeetingReminders).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: 'Re-extract' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /All conversations/ })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('UploadChatDialog', () => {
  async function pickProvider(label: string) {
    await userEvent.click(screen.getByRole('button', { name: /Source/ }));
    await userEvent.click(await screen.findByRole('option', { name: label }));
  }

  it('uploads a Slack export with its users.json', async () => {
    client.uploadChatThread.mockResolvedValue(chatDetail());
    const onUploaded = vi.fn();
    wrap(<UploadChatDialog scope={SCOPE} onClose={vi.fn()} onUploaded={onUploaded} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Channel name'), '#infra');
    fireEvent.change(screen.getByLabelText('Export'), { target: { value: SLACK_WITH_CHANNEL } });
    // A Slack export with a channel object needs no attestation.
    expect(screen.queryByRole('checkbox', { name: /from a channel/ })).not.toBeInTheDocument();
    await user.upload(screen.getByLabelText('Slack users.json file'), new File(['[{"id":"U1"}]'], 'users.json', { type: 'application/json' }));
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
    expect(client.uploadChatThread).toHaveBeenCalledWith(SCOPE, {
      provider: 'slack',
      channel_name: '#infra',
      content: SLACK_WITH_CHANNEL,
      users: '[{"id":"U1"}]',
      extract: true,
    });
  });

  it('requires the attestation for a paste and sends it', async () => {
    client.uploadChatThread.mockResolvedValue(chatDetail());
    wrap(<UploadChatDialog scope={SCOPE} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    await pickProvider('Pasted text');
    expect(screen.queryByLabelText('Slack users.json file')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('Channel name'), '#infra');
    fireEvent.change(screen.getByLabelText('Thread'), { target: { value: 'Tolu: hi' } });
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    expect(await screen.findByText(/not a direct message/, { selector: '[role="alert"], [role="alert"] *' })).toBeInTheDocument();
    expect(client.uploadChatThread).not.toHaveBeenCalled();
    await user.click(screen.getByRole('checkbox', { name: /from a channel/ }));
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    await waitFor(() =>
      expect(client.uploadChatThread).toHaveBeenCalledWith(SCOPE, expect.objectContaining({ provider: 'paste', attestation: true })),
    );
  });

  it('requires the attestation for a bare Slack message array and sends it', async () => {
    client.uploadChatThread.mockResolvedValue(chatDetail());
    wrap(<UploadChatDialog scope={SCOPE} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Channel name'), '#infra');
    fireEvent.change(screen.getByLabelText('Export'), { target: { value: '[{"text":"hi"}]' } });
    expect(screen.getByRole('checkbox', { name: /from a channel/ })).toBeInTheDocument();
    expect(screen.getByText(/no channel details/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    expect(await screen.findByText(/not a direct message/, { selector: '[role="alert"], [role="alert"] *' })).toBeInTheDocument();
    expect(client.uploadChatThread).not.toHaveBeenCalled();
    await user.click(screen.getByRole('checkbox', { name: /from a channel/ }));
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    await waitFor(() =>
      expect(client.uploadChatThread).toHaveBeenCalledWith(SCOPE, expect.objectContaining({ provider: 'slack', attestation: true })),
    );
  });

  it('refuses over 2 MiB before sending, and shows a server error inline', async () => {
    wrap(<UploadChatDialog scope={SCOPE} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Channel name'), '#infra');
    fireEvent.change(screen.getByLabelText('Export'), { target: { value: 'x'.repeat(MAX_CHAT_BYTES + 1) } });
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    expect(await screen.findByText(/larger than 2 MB/)).toBeInTheDocument();
    expect(client.uploadChatThread).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Export'), { target: { value: SLACK_WITH_CHANNEL } });
    client.uploadChatThread.mockRejectedValue(apiError(422, { code: 'dm_not_supported' }));
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    expect(await screen.findByText(/Direct-message and group-DM exports/)).toBeInTheDocument();
  });

  it('locks its controls while submitting', async () => {
    client.uploadChatThread.mockReturnValue(new Promise(() => {}));
    const onClose = vi.fn();
    wrap(<UploadChatDialog scope={SCOPE} onClose={onClose} onUploaded={vi.fn()} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Channel name'), '#infra');
    fireEvent.change(screen.getByLabelText('Export'), { target: { value: SLACK_WITH_CHANNEL } });
    await user.click(screen.getByRole('button', { name: 'Add chat thread' }));
    await waitFor(() => expect(screen.getByLabelText('Channel name')).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  });

  it('has no axe violations', async () => {
    const { baseElement } = wrap(<UploadChatDialog scope={SCOPE} scopeLabel="Acme" onClose={vi.fn()} onUploaded={vi.fn()} />);
    expect(await axe(baseElement)).toHaveNoViolations();
  });
});

describe('Chat promote preview', () => {
  it('marks an owner matched by exporter email', async () => {
    client.previewMeetingPromote.mockResolvedValue({
      meeting_id: CHAT_ID,
      item_id: 'mai_1',
      title: 'Own the Dataproc ticket',
      description: 'Own the Dataproc ticket',
      priority: 'medium',
      kind: 'task',
      assigned_to: null,
      assigned_user_id: 'u_sam',
      due_date: null,
      context_refs: [],
      target_project_id: 'proj_1',
      owner_match_note: 'matched by exporter email',
    });
    client.listAssignableUsers.mockResolvedValue({ users: [{ user_id: 'u_sam', email: 'sam@acme.io', display_name: 'Sam' }] });
    wrap(<PromoteDialog meetingId={CHAT_ID} item={chatDetail().action_items[0]} chat onClose={vi.fn()} />);
    expect(await screen.findByText('Owner matched by exporter email.')).toBeInTheDocument();
  });
});
