import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HelperPanel from './HelperPanel';
import { axe } from '../test/axe';

/**
 * Coverage for the session-helper drawer. The panel streams answers with raw
 * `fetch` + `ReadableStream` (EventSource can't send `Authorization`), so the
 * suite drives that path directly: a mocked `fetch` returns an SSE body and we
 * assert the assistant text, tool steps, citations and errors render as the
 * frames arrive. Hooks are mocked one layer up so no provider tree is needed.
 */

const {
  useAuthMock,
  useMeMock,
  useHelperStatusMock,
  useHelperConversationsMock,
  useDeleteHelperConversationMock,
  getHelperConversationMock,
  deleteMutateMock,
  conversationsRefetchMock,
} = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  useMeMock: vi.fn(),
  useHelperStatusMock: vi.fn(),
  useHelperConversationsMock: vi.fn(),
  useDeleteHelperConversationMock: vi.fn(),
  getHelperConversationMock: vi.fn(),
  deleteMutateMock: vi.fn(),
  conversationsRefetchMock: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({ useAuth: useAuthMock }));
vi.mock('../hooks/useMe', () => ({ useMe: useMeMock }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('./useHelper', () => ({
  useHelperStatus: useHelperStatusMock,
  useHelperConversations: useHelperConversationsMock,
  useHelperConversation: () => ({ data: null, isLoading: false }),
  useDeleteHelperConversation: useDeleteHelperConversationMock,
}));
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

// ── SSE body helpers ──

const encoder = new TextEncoder();

function framesBody(frames: Array<[string, unknown]>): ReadableStream<Uint8Array> {
  const text = frames
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join('');
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

function okResponse(frames: Array<[string, unknown]>) {
  return new Response(framesBody(frames), { status: 200 });
}

function chunkedResponse(chunks: string[]) {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(encoder.encode(c));
        controller.close();
      },
    }),
    { status: 200 },
  );
}

function abortError() {
  return Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' });
}

/** A body that yields one text frame, then hangs until the signal aborts. */
function partialThenHang(signal: AbortSignal) {
  const body = {
    getReader() {
      let first = true;
      return {
        read: () => {
          if (first) {
            first = false;
            return Promise.resolve({
              done: false,
              value: encoder.encode('event: text\ndata: {"delta":"partial"}\n\n'),
            });
          }
          return new Promise<{ done: boolean; value?: Uint8Array }>((_, reject) => {
            if (signal.aborted) reject(abortError());
            else signal.addEventListener('abort', () => reject(abortError()), { once: true });
          });
        },
      };
    },
  };
  return { ok: true, status: 200, body, text: () => Promise.resolve('') };
}

/** A body that emits the given frames, then hangs until the signal aborts. */
function framesThenHang(frames: Array<[string, unknown]>, signal: AbortSignal) {
  const queued = frames
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join('');
  const body = {
    getReader() {
      let sent = false;
      return {
        read: () => {
          if (!sent) {
            sent = true;
            return Promise.resolve({ done: false, value: encoder.encode(queued) });
          }
          return new Promise<{ done: boolean; value?: Uint8Array }>((_, reject) => {
            if (signal.aborted) reject(abortError());
            else signal.addEventListener('abort', () => reject(abortError()), { once: true });
          });
        },
      };
    },
  };
  return { ok: true, status: 200, body, text: () => Promise.resolve('') };
}

let fetchMock: ReturnType<typeof vi.fn>;

function renderPanel(
  scope?: { project_id?: string; session_ids?: string[] },
  onClose: () => void = () => {},
) {
  return render(
    <MemoryRouter>
      <HelperPanel open onClose={onClose} scope={scope} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);

  useAuthMock.mockReturnValue({
    auth: {
      apiKey: 'sk_test',
      baseUrl: 'http://test.api',
      client: { getHelperConversation: getHelperConversationMock },
    },
  });
  useMeMock.mockReturnValue({
    data: { org_id: 'org_1', org_role: 'admin', email: 'admin@x.test', tier: 'admin' },
  });
  useHelperStatusMock.mockReturnValue({
    data: { enabled: true, key_source: 'user', provider: 'anthropic', model: 'claude' },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  useHelperConversationsMock.mockReturnValue({
    data: [],
    isLoading: false,
    refetch: conversationsRefetchMock,
  });
  useDeleteHelperConversationMock.mockReturnValue({
    mutate: deleteMutateMock,
    isPending: false,
    isError: false,
    error: null,
  });
  getHelperConversationMock.mockReset();
  deleteMutateMock.mockReset();
  conversationsRefetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HelperPanel', () => {
  it('streams an answer, folding in tool steps and citations', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_1' }],
        ['text', { delta: 'Hello' }],
        ['text', { delta: ' world' }],
        ['tool_result', { id: 't1', name: 'search_sessions', ok: true, item_count: 3 }],
        ['citation', { session_id: 'ses_1', message_index: 2, title: 'Debug auth middleware' }],
        ['done', { stop_reason: 'complete', input_tokens: 10, output_tokens: 5, key_source: 'user' }],
      ]),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'What failed?');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('Hello world')).toBeInTheDocument();
    });

    // Tool step rendered compactly: name + ok + item count.
    expect(screen.getByText('search_sessions')).toBeInTheDocument();
    expect(screen.getByText('· 3 items')).toBeInTheDocument();
    // Citation links to the session only — no #msg-N anchor (the index is a
    // raw messages.jsonl line, not a dashboard render anchor).
    const citation = screen.getByRole('link', { name: 'Debug auth middleware' });
    expect(citation).toHaveAttribute('href', '/sessions/ses_1');

    // The new conversation id was recorded and the list refetched.
    expect(conversationsRefetchMock).toHaveBeenCalled();

    // The stream request carried the right auth + SSE accept headers and body.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers).toMatchObject({
      Authorization: 'Bearer sk_test',
      Accept: 'text/event-stream',
    });
    expect(JSON.parse(init.body as string)).toEqual({ message: 'What failed?', stream: true });
  });

  it('assembles text split across stream chunks', async () => {
    fetchMock.mockResolvedValue(
      chunkedResponse([
        'event: text\ndata: {"delta":"Hel"}\n\n',
        'event: text\ndata: {"delta":"lo"}\n\n',
      ]),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'hi');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeInTheDocument();
    });
  });

  it('sends the active scope on a fresh conversation', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_1' }],
        ['text', { delta: 'hi' }],
      ]),
    );
    renderPanel({ session_ids: ['ses_1'] });
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'summarize');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('hi')).toBeInTheDocument();
    });
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body as string).scope).toEqual({ session_ids: ['ses_1'] });
    expect(screen.getByText('About this session')).toBeInTheDocument();
  });

  it('keeps History and New available while a conversation is open', async () => {
    useHelperConversationsMock.mockReturnValue({
      data: [
        { id: 'c1', org_id: null, title: 'First thread', created_at: '', updated_at: '' },
      ],
      isLoading: false,
      refetch: conversationsRefetchMock,
    });
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_1' }],
        ['text', { delta: 'Hello' }],
      ]),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'What failed?');
    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeInTheDocument();
    });

    // History and New stay available while the transcript is shown.
    expect(screen.getByRole('button', { name: 'New' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'First thread' })).toBeInTheDocument();
  });

  it('citation links to the session only (no #msg anchor) and close the drawer', async () => {
    const onClose = vi.fn();
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_1' }],
        ['text', { delta: 'See' }],
        ['citation', { session_id: 'ses_1', message_index: 2, title: 'Debug auth middleware' }],
      ]),
    );
    renderPanel(undefined, onClose);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'Where?');
    await user.keyboard('{Enter}');
    const link = await screen.findByRole('link', { name: 'Debug auth middleware' });

    // No #msg-N anchor: the index is a raw messages.jsonl line, not a render
    // anchor, so the link points at the session only.
    expect(link).toHaveAttribute('href', '/sessions/ses_1');
    await user.click(link);
    expect(onClose).toHaveBeenCalled();
  });

  it('does not leak a stale conversation id after the drawer is reset mid-turn', async () => {
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(
        framesThenHang(
          [
            ['conversation', { conversation_id: 'conv_A' }],
            ['text', { delta: 'partial' }],
          ],
          init.signal as AbortSignal,
        ),
      ),
    );

    const { rerender } = render(
      <MemoryRouter>
        <HelperPanel open onClose={vi.fn()} scope={{ session_ids: ['ses_1'] }} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'first');
    await user.keyboard('{Enter}');

    // The session-scoped turn has assigned conv_A and is now hanging.
    await waitFor(() => {
      expect(screen.getByText('partial')).toBeInTheDocument();
    });

    // Close the drawer, then reopen it scoped to a project. The reopen aborts
    // the in-flight turn and must not let its finally block write conv_A back.
    rerender(
      <MemoryRouter>
        <HelperPanel open={false} onClose={vi.fn()} scope={{ session_ids: ['ses_1'] }} />
      </MemoryRouter>,
    );
    rerender(
      <MemoryRouter>
        <HelperPanel open onClose={vi.fn()} scope={{ project_id: 'proj_1' }} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText('Ask the session helper'), 'second');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    const [, init] = fetchMock.mock.calls[1];
    const body = JSON.parse(init.body as string);
    expect(body.conversation_id).toBeUndefined();
    expect(body.scope).toEqual({ project_id: 'proj_1' });
  });

  it('New during a stream unlocks the composer', async () => {
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(partialThenHang(init.signal as AbortSignal)),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'What failed?');
    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(screen.getByText('partial')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'New' }));

    // The aborted turn no longer owns the composer; it must be usable again.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Ask the session helper')).not.toBeDisabled();
  });

  it('Stop aborts the in-flight stream and keeps the partial answer', async () => {
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(partialThenHang(init.signal as AbortSignal)),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'What failed?');
    await user.keyboard('{Enter}');

    // First frame arrived; stream is now hanging and the composer shows Stop.
    await waitFor(() => {
      expect(screen.getByText('partial')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'Stop' }));

    // Composer returns to Send; the partial text is retained and no error is
    // surfaced for a user-initiated abort (the server persists the partial).
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
    });
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.queryByText(/aborted/i)).not.toBeInTheDocument();
  });

  it('shows an error event from the stream alongside any partial text', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['text', { delta: 'partial' }],
        ['error', { code: 'provider_error', message: 'The model is unavailable.' }],
      ]),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'What failed?');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('The model is unavailable.')).toBeInTheDocument();
    });
    expect(screen.getByText('partial')).toBeInTheDocument();
  });

  it('surfaces a transport error when the request fails', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      body: null,
      text: () => Promise.resolve('{"error":{"code":"helper_error","message":"boom"}}'),
    });
    renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'What failed?');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('boom')).toBeInTheDocument();
    });
  });

  it('shows the Enterprise upsell when the helper is disabled', () => {
    useHelperStatusMock.mockReturnValue({
      data: { enabled: false, key_source: null, provider: null, model: null },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    renderPanel();

    expect(screen.getByText(/available on the Enterprise plan/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View plans' })).toHaveAttribute(
      'href',
      '/settings/billing',
    );
  });

  it('prompts for a key, adding an org-admin hint for org members', () => {
    useHelperStatusMock.mockReturnValue({
      data: { enabled: true, key_source: null, provider: null, model: null },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    useMeMock.mockReturnValue({
      data: { org_id: 'org_1', org_role: 'member', email: 'm@x.test', tier: 'team' },
    });
    renderPanel();

    expect(screen.getByText(/Add a key in Settings → Data protection → Security review model/i)).toBeInTheDocument();
    expect(screen.getByText(/Or ask your org admin to add an org key/i)).toBeInTheDocument();
  });

  it('omits the org-admin hint for users without an org', () => {
    useHelperStatusMock.mockReturnValue({
      data: { enabled: true, key_source: null, provider: null, model: null },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    useMeMock.mockReturnValue({ data: { email: 'solo@x.test', tier: 'free' } });
    renderPanel();

    expect(screen.getByText(/Add a key in Settings → Data protection → Security review model/i)).toBeInTheDocument();
    expect(screen.queryByText(/Or ask your org admin/i)).not.toBeInTheDocument();
  });

  it('lists conversations and continues one on click', async () => {
    useHelperConversationsMock.mockReturnValue({
      data: [
        { id: 'c1', org_id: null, title: 'First thread', created_at: '', updated_at: '' },
        { id: 'c2', org_id: null, title: null, created_at: '', updated_at: '' },
      ],
      isLoading: false,
      refetch: conversationsRefetchMock,
    });
    getHelperConversationMock.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'First thread',
      created_at: '',
      updated_at: '',
      messages: [
        { id: 'm1', role: 'user', content: 'question?' },
        { id: 'm2', role: 'assistant', content: 'the answer' },
      ],
    });
    renderPanel();
    const user = userEvent.setup();

    expect(screen.getByRole('button', { name: 'First thread' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Untitled conversation' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'First thread' }));

    await waitFor(() => {
      expect(getHelperConversationMock).toHaveBeenCalledWith('c1');
    });
    expect(await screen.findByText('the answer')).toBeInTheDocument();
    expect(screen.getByText('question?')).toBeInTheDocument();
  });

  it('restores the stored scope when continuing a scoped thread', async () => {
    useHelperConversationsMock.mockReturnValue({
      data: [
        { id: 'c1', org_id: 'org_1', title: 'Project thread', created_at: '', updated_at: '' },
      ],
      isLoading: false,
      refetch: conversationsRefetchMock,
    });
    getHelperConversationMock.mockResolvedValue({
      id: 'c1',
      org_id: 'org_1',
      title: 'Project thread',
      created_at: '',
      updated_at: '',
      scope: { project_id: 'proj_1', session_ids: [] },
      messages: [
        { id: 'm1', role: 'user', content: 'question?' },
        { id: 'm2', role: 'assistant', content: 'the answer' },
      ],
    });
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'c1' }],
        ['text', { delta: 'again' }],
      ]),
    );
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Project thread' }));
    await screen.findByText('the answer');

    // The restored scope is shown and re-sent on the follow-up question.
    expect(screen.getByText('About this project')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Ask the session helper'), 'again?');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('again')).toBeInTheDocument();
    });
    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body as string);
    expect(body.conversation_id).toBe('c1');
    expect(body.scope).toEqual({ project_id: 'proj_1', session_ids: [] });
  });

  it('deletes a conversation after confirming', async () => {
    useHelperConversationsMock.mockReturnValue({
      data: [{ id: 'c1', org_id: null, title: 'Doomed', created_at: '', updated_at: '' }],
      isLoading: false,
      refetch: conversationsRefetchMock,
    });
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Delete Doomed' }));

    expect(await screen.findByText(/Delete conversation\?/i)).toBeInTheDocument();
    expect(screen.getByText(/permanently deletes/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Delete' }));

    expect(deleteMutateMock).toHaveBeenCalledWith('c1', expect.any(Object));
  });

  it('Escape closes only the delete dialog, not the drawer', async () => {
    useHelperConversationsMock.mockReturnValue({
      data: [{ id: 'c1', org_id: null, title: 'Doomed', created_at: '', updated_at: '' }],
      isLoading: false,
      refetch: conversationsRefetchMock,
    });
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Delete Doomed' }));
    expect(await screen.findByText(/Delete conversation\?/i)).toBeInTheDocument();

    await user.keyboard('{Escape}');

    await waitFor(() => {
      expect(screen.queryByText(/Delete conversation\?/i)).not.toBeInTheDocument();
    });
    // The drawer stays open — only the nested dialog was dismissed.
    expect(screen.getByRole('heading', { name: 'Session helper' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Doomed' })).toBeInTheDocument();
  });

  it('marks the transcript busy while a turn is streaming', async () => {
    fetchMock.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(partialThenHang(init.signal as AbortSignal)),
    );
    const { container } = renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'hi');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('partial')).toBeInTheDocument();
    });

    expect(container.querySelector('[aria-live="polite"]')).toHaveAttribute('aria-busy', 'true');
  });

  it('clears busy and announces completion once the stream finishes', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['text', { delta: 'Hello' }],
        ['done', { stop_reason: 'complete' }],
      ]),
    );
    const { container } = renderPanel();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Ask the session helper'), 'hi');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.getByText('Hello')).toBeInTheDocument();
    });

    // The polite status region announces completion to screen readers, and the
    // busy flag clears once the stream ends.
    expect(screen.getByRole('status')).toHaveTextContent('Answer complete');
    expect(container.querySelector('[aria-live="polite"]')).toHaveAttribute('aria-busy', 'false');
  });

  it('has no axe violations in the enabled chat state', async () => {
    const { container } = renderPanel();
    expect(await axe(container)).toHaveNoViolations();
  });
});
