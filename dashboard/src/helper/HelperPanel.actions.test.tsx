import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The drawer attaches write-action proposals to the answer they arrived in:
 * live from the `action_proposed` SSE event, and on a restored thread from
 * the stored tool result ("Proposed action ha_…"), whose status is then read
 * back from the server. The action hooks run for real; the rest is stubbed.
 */

const { useAuthMock, useHelperStatusMock, useHelperConversationsMock, getHelperConversation, getHelperAction } =
  vi.hoisted(() => ({
    useAuthMock: vi.fn(),
    useHelperStatusMock: vi.fn(),
    useHelperConversationsMock: vi.fn(),
    getHelperConversation: vi.fn(),
    getHelperAction: vi.fn(),
  }));

vi.mock('../auth/AuthContext', () => ({ useAuth: useAuthMock }));
vi.mock('../hooks/useMe', () => ({ useMe: () => ({ data: { org_id: null } }) }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('./useHelper', async (importActual) => ({
  ...(await importActual<typeof import('./useHelper')>()),
  useHelperStatus: useHelperStatusMock,
  useHelperConversations: useHelperConversationsMock,
  useDeleteHelperConversation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import HelperPanel from './HelperPanel';

const ACTION_ID = 'ha_0123456789abcdef01234567';

function okResponse(frames: Array<[string, unknown]>) {
  const text = frames.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('');
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode(text));
        c.close();
      },
    }),
    { status: 200 },
  );
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HelperPanel open onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({
    auth: { apiKey: 'sk', baseUrl: 'http://test.api', client: { getHelperConversation, getHelperAction } },
  });
  useHelperStatusMock.mockReturnValue({
    data: { enabled: true, key_source: 'user', provider: 'anthropic', model: 'claude' },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  useHelperConversationsMock.mockReturnValue({
    data: [{ id: 'c1', org_id: null, title: 'Cleanup', created_at: '', updated_at: new Date().toISOString() }],
    isLoading: false,
    refetch: vi.fn(),
  });
});

afterEach(() => vi.unstubAllGlobals());

describe('HelperPanel — write actions', () => {
  it('renders a streamed action_proposed event as a confirm card in that answer', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        okResponse([
          ['conversation', { conversation_id: 'hc_1' }],
          ['tool_call', { id: 't1', name: 'trash_sessions', args: { session_ids: ['ses_a'] } }],
          ['tool_result', { id: 't1', name: 'trash_sessions', ok: true, item_count: 1 }],
          [
            'action_proposed',
            {
              action: {
                id: ACTION_ID,
                kind: 'trash_sessions',
                summary: 'Move 1 session(s) to Trash',
                targets: [{ id: 'ses_a', name: 'Old spike', change: 'will_change', before: 'active', after: 'trashed' }],
                expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
              },
            },
          ],
          ['text', { delta: 'I proposed moving it to Trash.' }],
          ['done', { stop_reason: 'stop' }],
        ]),
      ),
    );
    renderPanel();
    await userEvent.type(screen.getByRole('textbox', { name: 'Ask the session helper' }), 'clean up{Enter}');
    const conversation = await screen.findByRole('region', { name: 'Conversation' });
    expect(await within(conversation).findByRole('heading', { name: 'Move 1 session to Trash' })).toBeInTheDocument();
    expect(within(conversation).getByRole('button', { name: 'Move to Trash' })).toBeInTheDocument();
    expect(getHelperAction).not.toHaveBeenCalled();
  });

  it('recovers a proposal from a restored thread and reads its status', async () => {
    getHelperConversation.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'Cleanup',
      created_at: '',
      updated_at: '',
      scope: null,
      messages: [
        { id: 'm1', role: 'user', content: 'turn narrative on' },
        {
          id: 't1',
          role: 'tool',
          content: `Proposed action ${ACTION_ID} — waiting for the user to confirm. Do not claim it is done.`,
          tool_name: 'set_project_auto_narrative',
          tool_call_id: 'tc1',
          tool_args: {},
        },
        { id: 'm2', role: 'assistant', content: 'Proposed.', citations: null },
        {
          id: 'n1',
          role: 'note',
          content: `[Action ${ACTION_ID} (set_project_auto_narrative) confirmed: 1 changed, 0 failed]`,
          tool_name: null,
          tool_call_id: null,
          tool_args: { action_id: ACTION_ID, status: 'confirmed' },
        },
      ],
    });
    getHelperAction.mockResolvedValue({
      id: ACTION_ID,
      kind: 'set_auto_narrative',
      status: 'confirmed',
      summary: 'Set auto-narrative on for 1 project(s)',
      targets: [{ id: 'proj_1', name: 'Acme', change: 'will_change', before: false, after: true }],
      params: { project_ids: ['proj_1'], enabled: true },
      result: { targets: [{ id: 'proj_1', ok: true, error: null }] },
      created_at: null,
      expires_at: null,
      decided_at: null,
    });
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /Cleanup/ }));
    expect(await screen.findByRole('heading', { name: 'Turn Auto Narrative on for 1 project' })).toBeInTheDocument();
    expect(getHelperAction).toHaveBeenCalledWith(ACTION_ID);
    expect(screen.getByText('Done')).toBeInTheDocument();
    // The outcome note is for the model, not something to show.
    expect(screen.queryByText(/changed, 0 failed/)).not.toBeInTheDocument();
  });

  it('recovers a ticket proposal with the right action kind', async () => {
    getHelperConversation.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'Assign',
      created_at: '',
      updated_at: '',
      scope: null,
      messages: [
        { id: 'm1', role: 'user', content: 'assign it' },
        {
          id: 't1',
          role: 'tool',
          content: `Proposed action ${ACTION_ID} — waiting for the user to confirm. Do not claim it is done.`,
          tool_name: 'propose_assignment',
          tool_call_id: 'tc1',
          tool_args: {},
        },
        { id: 'm2', role: 'assistant', content: 'Proposed.', citations: null },
      ],
    });
    getHelperAction.mockResolvedValue({
      id: ACTION_ID,
      kind: 'assign_ticket',
      status: 'pending',
      summary: 'Assign Fix webhook retry to Atlas',
      targets: [{ id: 'tk_1', name: 'Fix webhook retry', change: 'will_change', before: null, after: 'Atlas' }],
      params: { ticket_id: 'tk_1', assigned_to: 'Atlas' },
      result: {},
      created_at: null,
      expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
      decided_at: null,
    });
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /Cleanup/ }));
    expect(await screen.findByRole('heading', { name: 'Assign to Atlas' })).toBeInTheDocument();
    expect(screen.getByText(/unassigned → Atlas/)).toBeInTheDocument();
    expect(getHelperAction).toHaveBeenCalledWith(ACTION_ID);
  });

  it('prefers the structured action_id over the result text', async () => {
    getHelperConversation.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'Cleanup',
      created_at: '',
      updated_at: '',
      scope: null,
      messages: [
        { id: 'm1', role: 'user', content: 'clean up' },
        {
          id: 't1',
          role: 'tool',
          // A redacted or reworded result: the text prefix can't be relied on.
          content: 'Targets: [REDACTED:ssn]',
          tool_name: 'trash_sessions',
          tool_call_id: 'tc1',
          tool_args: {},
          action_id: ACTION_ID,
        },
        { id: 'm2', role: 'assistant', content: 'Proposed.', citations: null },
      ],
    });
    getHelperAction.mockResolvedValue({
      id: ACTION_ID,
      kind: 'trash_sessions',
      status: 'pending',
      summary: 'Move 1 session(s) to Trash',
      targets: [{ id: 'ses_a', name: 'Old spike', change: 'will_change', before: 'active', after: 'trashed' }],
      params: { session_ids: ['ses_a'] },
      result: {},
      created_at: null,
      expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
      decided_at: null,
    });
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /Cleanup/ }));
    expect(await screen.findByRole('heading', { name: 'Move 1 session to Trash' })).toBeInTheDocument();
    expect(getHelperAction).toHaveBeenCalledWith(ACTION_ID);
  });
});
