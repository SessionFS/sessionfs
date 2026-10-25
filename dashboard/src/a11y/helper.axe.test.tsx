import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HelperPanel from '../helper/HelperPanel';
import { axe } from '../test/axe';

// The session-helper drawer audited in each of its designed states, with its
// data hooks stubbed (same approach as helper/HelperPanel.test.tsx) and the
// answer streamed through a mocked fetch so steps, chips and sources render.

const { useHelperStatusMock, useHelperConversationsMock, useMeMock } = vi.hoisted(() => ({
  useHelperStatusMock: vi.fn(),
  useHelperConversationsMock: vi.fn(),
  useMeMock: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { apiKey: 'sk_test', baseUrl: 'http://test.api', client: {} } }),
}));
vi.mock('../hooks/useMe', () => ({ useMe: useMeMock }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('../helper/useHelper', () => ({
  useHelperStatus: useHelperStatusMock,
  useHelperConversations: useHelperConversationsMock,
  useHelperConversation: () => ({ data: null, isLoading: false }),
  useDeleteHelperConversation: () => ({ mutate: vi.fn(), isPending: false }),
}));

const status = (data: { enabled: boolean; key_source: string | null }) => ({
  data: { provider: 'anthropic', model: 'claude', ...data },
  isLoading: false,
  isError: false,
  refetch: vi.fn(),
});

function streamOf(frames: Array<[string, unknown]>) {
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
  return render(
    <MemoryRouter>
      <HelperPanel open onClose={() => {}} scope={{ project_id: 'proj_1', label: 'Acme Widgets' }} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useHelperStatusMock.mockReturnValue(status({ enabled: true, key_source: 'user' }));
  useHelperConversationsMock.mockReturnValue({
    data: [
      { id: 'c1', org_id: 'org_1', title: 'Why did the webhook retry?', created_at: '', updated_at: new Date().toISOString() },
      { id: 'c2', org_id: null, title: null, created_at: '', updated_at: '2020-01-01T00:00:00Z' },
    ],
    isLoading: false,
    refetch: vi.fn(),
  });
  useMeMock.mockReturnValue({ data: { org_id: 'org_1', org_role: 'member', org_name: 'Acme' } });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('session helper drawer has no axe violations', () => {
  it('empty state with suggestions and recent conversations', async () => {
    const { baseElement } = renderPanel();
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('answered turn with tool steps, citation chips and sources', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        streamOf([
          ['conversation', { conversation_id: 'hc_1' }],
          ['tool_call', { id: 't1', name: 'search_sessions', args: { query: 'retry' } }],
          ['tool_result', { id: 't1', name: 'search_sessions', ok: true, item_count: 2 }],
          ['text', { delta: '**Late ack** [session:ses_1 msg:3].\n\n```py\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |' }],
          ['citation', { session_id: 'ses_1', message_index: 3, title: 'Fix webhook' }],
          ['done', { stop_reason: 'stop' }],
        ]),
      ),
    );
    const { baseElement } = renderPanel();
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: 'Ask the session helper' }), 'why?{Enter}');
    await screen.findByRole('list', { name: 'Sources' });
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('error card after a failed turn', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        body: null,
        text: () => Promise.resolve('{"detail":{"code":"helper_busy","message":"busy"}}'),
      }),
    );
    const { baseElement } = renderPanel();
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: 'Ask the session helper' }), 'why?{Enter}');
    await screen.findByText('The helper is busy');
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('history view', async () => {
    const { baseElement } = renderPanel();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Conversation history' }));
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('scope menu open', async () => {
    const { baseElement } = renderPanel();
    await userEvent.setup().click(screen.getByRole('button', { name: /Change scope/ }));
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('missing key and not-entitled states', async () => {
    useHelperStatusMock.mockReturnValue(status({ enabled: true, key_source: null }));
    const first = renderPanel();
    expect(await axe(first.baseElement)).toHaveNoViolations();
    first.unmount();

    useHelperStatusMock.mockReturnValue(status({ enabled: false, key_source: null }));
    const second = renderPanel();
    expect(await axe(second.baseElement)).toHaveNoViolations();
  });
});
