import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import HelperPanel from './HelperPanel';
import type { HelperScope } from './HelperProvider';

/**
 * Coverage for the session-helper drawer. The panel streams answers with raw
 * `fetch` + `ReadableStream` (EventSource can't send `Authorization`), so the
 * suite drives that path directly: a mocked `fetch` returns an SSE body and we
 * assert the assistant text, tool steps, citations and errors render as the
 * frames arrive. Hooks are mocked one layer up so no provider tree is needed;
 * markdown renders for real so citation chips are exercised end to end.
 * axe coverage for the drawer's states lives in src/a11y/helper.axe.test.tsx.
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
  statusRefetchMock,
} = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  useMeMock: vi.fn(),
  useHelperStatusMock: vi.fn(),
  useHelperConversationsMock: vi.fn(),
  useDeleteHelperConversationMock: vi.fn(),
  getHelperConversationMock: vi.fn(),
  deleteMutateMock: vi.fn(),
  conversationsRefetchMock: vi.fn(),
  statusRefetchMock: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({ useAuth: useAuthMock }));
vi.mock('../hooks/useMe', () => ({ useMe: useMeMock }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('../hooks/useProjects', () => ({ useProject: () => ({ data: undefined }) }));
vi.mock('./useHelper', () => ({
  useHelperStatus: useHelperStatusMock,
  useHelperConversations: useHelperConversationsMock,
  useHelperConversation: () => ({ data: null, isLoading: false }),
  useDeleteHelperConversation: useDeleteHelperConversationMock,
}));

// ── SSE body helpers ──

const encoder = new TextEncoder();
type Frame = [string, unknown];

function sse(frames: Frame[]): string {
  return frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');
}

function okResponse(frames: Frame[]) {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(sse(frames)));
        controller.close();
      },
    }),
    { status: 200 },
  );
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

function errorResponse(status: number, body: unknown) {
  return { ok: false, status, body: null, text: () => Promise.resolve(JSON.stringify(body)) };
}

function abortError() {
  return Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' });
}

/** A body that emits the given frames, then hangs until the signal aborts. */
function framesThenHang(frames: Frame[], signal: AbortSignal) {
  const queued = sse(frames);
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

const hangAfter = (frames: Frame[]) => (_url: string, init: RequestInit) =>
  Promise.resolve(framesThenHang(frames, init.signal as AbortSignal));

const DONE: Frame = ['done', { stop_reason: 'stop', input_tokens: 10, output_tokens: 5, key_source: 'user' }];

let fetchMock: ReturnType<typeof vi.fn>;

function renderPanel(scope?: HelperScope, onClose: () => void = () => {}) {
  return render(
    <MemoryRouter>
      <HelperPanel open onClose={onClose} scope={scope} />
    </MemoryRouter>,
  );
}

function composer() {
  return screen.getByRole('textbox', { name: 'Ask the session helper' });
}

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(composer(), text);
  await user.keyboard('{Enter}');
}

function sentBody(call = 0) {
  return JSON.parse(fetchMock.mock.calls[call][1].body as string);
}

function setStatus(data: { enabled: boolean; key_source: string | null }) {
  useHelperStatusMock.mockReturnValue({
    data: { provider: null, model: null, ...data },
    isLoading: false,
    isError: false,
    refetch: statusRefetchMock,
  });
}

function statusResult(
  data: { enabled: boolean; key_source: string | null } | undefined,
  opts: { isLoading?: boolean; isError?: boolean; isPlaceholderData?: boolean; error?: unknown } = {},
) {
  return {
    data: data ? { provider: null, model: null, ...data } : undefined,
    isLoading: opts.isLoading ?? false,
    isError: opts.isError ?? false,
    isPlaceholderData: opts.isPlaceholderData ?? false,
    error: opts.error,
    refetch: statusRefetchMock,
  };
}

function setConversations(data: Array<{ id: string; title: string | null; org_id?: string | null; updated_at?: string }>) {
  useHelperConversationsMock.mockReturnValue({
    data: data.map((c) => ({ org_id: null, created_at: '', updated_at: new Date().toISOString(), ...c })),
    isLoading: false,
    refetch: conversationsRefetchMock,
  });
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
    data: { org_id: 'org_1', org_role: 'admin', org_name: 'Acme', email: 'admin@x.test', tier: 'admin' },
  });
  useHelperStatusMock.mockReturnValue({
    data: { enabled: true, key_source: 'user', provider: 'anthropic', model: 'claude' },
    isLoading: false,
    isError: false,
    refetch: statusRefetchMock,
  });
  setConversations([]);
  useDeleteHelperConversationMock.mockReturnValue({
    mutate: deleteMutateMock,
    isPending: false,
    isError: false,
    error: null,
  });
  getHelperConversationMock.mockReset();
  deleteMutateMock.mockReset();
  conversationsRefetchMock.mockReset();
  statusRefetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HelperPanel — streaming', () => {
  it('starts a new paragraph for text from a later model step', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_steps' }],
        ['text', { delta: 'Checking which sessions ran on DeepSeek.' }],
        ['tool_call', { id: 't1', name: 'list_session_models', args: {} }],
        ['tool_result', { id: 't1', name: 'list_session_models', ok: true }],
        ['text', { delta: 'I can see two sessions.' }],
        DONE,
      ]),
    );
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'deepseek sessions?');

    const first = await screen.findByText('Checking which sessions ran on DeepSeek.');
    const second = screen.getByText('I can see two sessions.');
    expect(first.tagName).toBe('P');
    expect(second.tagName).toBe('P');
    expect(first).not.toBe(second);
  });

  it('does not double the break the server already streamed', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_steps2' }],
        ['text', { delta: 'First.' }],
        ['tool_call', { id: 't1', name: 'list_recent_sessions', args: { model: ['deepseek'] } }],
        ['tool_result', { id: 't1', name: 'list_recent_sessions', ok: true }],
        ['text', { delta: '\n\n' }],
        ['text', { delta: 'Second.' }],
        DONE,
      ]),
    );
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'q');
    const second = await screen.findByText('Second.');
    expect(second.tagName).toBe('P');
    expect(screen.getByRole('button', { name: /Listed sessions using “deepseek”/ })).toBeInTheDocument();
  });

  it('streams an answer with tool steps, inline citation chips and a sources row', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['conversation', { conversation_id: 'conv_1' }],
        ['tool_call', { id: 't1', name: 'search_sessions', args: { query: 'auth bug' } }],
        ['tool_result', { id: 't1', name: 'search_sessions', ok: true, item_count: 3 }],
        ['text', { delta: 'The middleware ' }],
        ['text', { delta: 'dropped the header [session:ses_1 msg:2].' }],
        ['citation', { session_id: 'ses_1', message_index: 2, title: 'Debug auth middleware' }],
        DONE,
      ]),
    );
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'What failed?');

    await screen.findByText(/The middleware dropped the header/);
    // The raw marker never reaches the reader.
    expect(screen.queryByText(/\[session:/)).not.toBeInTheDocument();

    // Tool activity collapses to a one-line step once the turn settles.
    const steps = screen.getByRole('button', { name: /Searched sessions for “auth bug”/ });
    expect(steps).toHaveTextContent('Searched sessions for “auth bug” · 3 results');
    expect(steps).toHaveAttribute('aria-expanded', 'false');

    // Inline footnote chip + sources row both deep-link to the cited message.
    const chip = screen.getByRole('link', { name: 'Source 1: Debug auth middleware, message 2' });
    expect(chip).toHaveAttribute('href', '/sessions/ses_1?msg=2');
    expect(chip).toHaveTextContent('1');
    const sources = screen.getByRole('list', { name: 'Sources' });
    expect(within(sources).getByRole('link', { name: /Debug auth middleware/ })).toHaveAttribute('href', '/sessions/ses_1?msg=2');

    // The new conversation id was recorded and the list refetched.
    expect(conversationsRefetchMock).toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Answer complete, with 1 source.');

    // The stream request carried the right auth + SSE accept headers and body.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('http://test.api/api/v1/helper/turns');
    expect(init.headers).toMatchObject({ Authorization: 'Bearer sk_test', Accept: 'text/event-stream' });
    expect(sentBody()).toEqual({ message: 'What failed?', stream: true });
  });

  it('assembles text split across stream chunks', async () => {
    fetchMock.mockResolvedValue(
      chunkedResponse([
        'event: text\ndata: {"delta":"Hel"}\n\n',
        'event: text\ndata: {"delta":"lo"}\n\n',
        'event: done\ndata: {"stop_reason":"stop"}\n\n',
      ]),
    );
    renderPanel();
    await ask(userEvent.setup(), 'hi');
    expect(await screen.findByText('Hello')).toBeInTheDocument();
  });

  it('shows a running tool step with its arguments while the answer is gathered', async () => {
    fetchMock.mockImplementation(
      hangAfter([
        ['conversation', { conversation_id: 'conv_1' }],
        ['tool_call', { id: 't1', name: 'search_sessions', args: { query: 'redis' } }],
        ['tool_result', { id: 't1', name: 'search_sessions', ok: true, item_count: 6 }],
        ['tool_call', { id: 't2', name: 'get_session_excerpts', args: { session_id: 'ses_9', query: 'locks' } }],
      ]),
    );
    renderPanel();
    await ask(userEvent.setup(), 'Why Redis?');

    const summary = await screen.findByRole('button', { name: /Reading session excerpts about “locks”…/ });
    // Open while running so the reader can follow along.
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    const list = screen.getByRole('list', { name: 'Steps' });
    expect(within(list).getByText('Searched sessions for “redis”')).toBeInTheDocument();
    expect(within(list).getByText('6 results')).toBeInTheDocument();
    // The section is busy and Stop replaces Send.
    expect(screen.getByRole('region', { name: 'Conversation' })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });

  it('expands and collapses settled tool steps', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['tool_call', { id: 't1', name: 'list_recent_sessions', args: {} }],
        ['tool_result', { id: 't1', name: 'list_recent_sessions', ok: true, item_count: 4 }],
        ['tool_call', { id: 't2', name: 'get_project_context', args: { project_id: 'p' } }],
        ['tool_result', { id: 't2', name: 'get_project_context', ok: false, item_count: 0 }],
        ['text', { delta: 'Done.' }],
        DONE,
      ]),
    );
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'recent?');
    const summary = await screen.findByRole('button', { name: /Used 2 tools/ });
    expect(summary).toHaveTextContent('Used 2 tools · 4 results · 1 failed');
    expect(screen.queryByRole('list', { name: 'Steps' })).not.toBeInTheDocument();
    await user.click(summary);
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    const list = screen.getByRole('list', { name: 'Steps' });
    expect(within(list).getByText('Listed recent sessions')).toBeInTheDocument();
    expect(within(list).getByText(/Read the project context/)).toBeInTheDocument();
    expect(within(list).getByText(/failed/)).toBeInTheDocument();
    await user.click(summary);
    expect(screen.queryByRole('list', { name: 'Steps' })).not.toBeInTheDocument();
  });

  it('citation links close the drawer when followed', async () => {
    const onClose = vi.fn();
    fetchMock.mockResolvedValue(
      okResponse([
        ['text', { delta: 'See [session:ses_1].' }],
        ['citation', { session_id: 'ses_1', message_index: null, title: 'Debug auth middleware' }],
        DONE,
      ]),
    );
    renderPanel(undefined, onClose);
    const user = userEvent.setup();
    await ask(user, 'Where?');
    const chip = await screen.findByRole('link', { name: 'Source 1: Debug auth middleware' });
    await user.click(chip);
    expect(onClose).toHaveBeenCalled();
  });

  it('links each chip to its own cited message and the source row to the first', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['text', { delta: 'First [session:ses_9 msg:14], later [session:ses_9 msg:3], overall [session:ses_9].' }],
        ['citation', { session_id: 'ses_9', message_index: 14, title: 'Refactor' }],
        ['citation', { session_id: 'ses_9', message_index: 3, title: 'Refactor' }],
        DONE,
      ]),
    );
    renderPanel();
    await ask(userEvent.setup(), 'q');
    expect(await screen.findByRole('link', { name: 'Source 1: Refactor, message 14' })).toHaveAttribute(
      'href',
      '/sessions/ses_9?msg=14',
    );
    expect(screen.getByRole('link', { name: 'Source 1: Refactor, message 3' })).toHaveAttribute('href', '/sessions/ses_9?msg=3');
    // A marker without msg: opens the session itself.
    expect(screen.getByRole('link', { name: 'Source 1: Refactor, messages 3, 14' })).toHaveAttribute('href', '/sessions/ses_9');
    const sources = screen.getByRole('list', { name: 'Sources' });
    expect(within(sources).getByRole('link', { name: /Refactor/ })).toHaveAttribute('href', '/sessions/ses_9?msg=3');
  });

  it('renders a marker the server did not confirm as an inert chip', async () => {
    fetchMock.mockResolvedValue(
      okResponse([['text', { delta: 'Mentioned in [session:ses_gone].' }], DONE]),
    );
    renderPanel();
    await ask(userEvent.setup(), 'q');
    await screen.findByText(/Mentioned in/);
    expect(screen.queryByRole('link', { name: /Source 1/ })).not.toBeInTheDocument();
    expect(screen.getByTitle('Source not available to you')).toHaveTextContent('1');
    expect(screen.queryByRole('list', { name: 'Sources' })).not.toBeInTheDocument();
  });

  it('copies the answer as plain text with its sources', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['text', { delta: 'Fixed in [session:ses_1 msg:4].' }],
        ['citation', { session_id: 'ses_1', message_index: 4, title: 'Fix login' }],
        DONE,
      ]),
    );
    renderPanel();
    // user-event installs a clipboard stub on navigator.
    const user = userEvent.setup();
    await ask(user, 'q');
    await user.click(await screen.findByRole('button', { name: 'Copy answer' }));
    await expect(navigator.clipboard.readText()).resolves.toBe(
      'Fixed in [1].\n\nSources:\n[1] Fix login (session ses_1)',
    );
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
  });
});

describe('HelperPanel — composer and keyboard', () => {
  it('focuses the prompt on open, sends on Enter and inserts a newline on Shift+Enter', async () => {
    fetchMock.mockResolvedValue(okResponse([['text', { delta: 'ok' }], DONE]));
    renderPanel();
    const user = userEvent.setup();
    expect(composer()).toHaveFocus();

    await user.type(composer(), 'line one');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(composer(), 'line two');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(composer()).toHaveValue('line one\nline two');

    await user.keyboard('{Enter}');
    await screen.findByText('ok');
    expect(sentBody().message).toBe('line one\nline two');
    expect(composer()).toHaveValue('');
  });

  it('disables Send for an empty prompt and over the server limit', async () => {
    renderPanel();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    fireEvent.change(composer(), { target: { value: 'x'.repeat(20_001) } });
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(screen.getByText('20,001 / 20,000')).toBeInTheDocument();
    expect(composer()).toHaveAttribute('aria-invalid', 'true');
  });

  it('Escape closes the drawer', async () => {
    const onClose = vi.fn();
    renderPanel(undefined, onClose);
    await userEvent.setup().keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('suggested prompts send immediately', async () => {
    fetchMock.mockResolvedValue(okResponse([['text', { delta: 'Here is a summary.' }], DONE]));
    renderPanel({ session_ids: ['ses_1'], label: 'Fix the login bug' });
    const user = userEvent.setup();
    expect(screen.getByRole('heading', { name: 'Ask about this session' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Summarize this session' }));
    await screen.findByText('Here is a summary.');
    expect(sentBody()).toEqual({ message: 'Summarize this session', stream: true, scope: { session_ids: ['ses_1'] } });
  });

  it('Stop aborts the in-flight stream and keeps the partial answer', async () => {
    fetchMock.mockImplementation(hangAfter([['text', { delta: 'partial' }]]));
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'What failed?');
    await screen.findByText('partial');

    await user.click(screen.getByRole('button', { name: 'Stop' }));
    await screen.findByRole('button', { name: 'Send' });
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.getByText(/Stopped — the partial answer is saved/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Answer stopped.');
  });

  it('New conversation during a stream unlocks the composer', async () => {
    fetchMock.mockImplementation(hangAfter([['text', { delta: 'partial' }]]));
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'What failed?');
    await screen.findByText('partial');
    await user.click(screen.getByRole('button', { name: 'New conversation' }));

    await screen.findByRole('button', { name: 'Send' });
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    expect(screen.queryByText('partial')).not.toBeInTheDocument();
    expect(composer()).not.toBeDisabled();
  });

  it('marks the conversation busy while streaming and announces completion once', async () => {
    fetchMock.mockResolvedValue(okResponse([['text', { delta: 'Hello' }], DONE]));
    renderPanel();
    await ask(userEvent.setup(), 'hi');
    await screen.findByText('Hello');
    expect(screen.getByRole('region', { name: 'Conversation' })).toHaveAttribute('aria-busy', 'false');
    // The polite region carries only the completion, never the streamed tokens.
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('Answer complete.');
    expect(status).not.toHaveTextContent('Hello');
  });
});

describe('HelperPanel — errors', () => {
  it('shows a stream error alongside partial text, and Retry asks again', async () => {
    fetchMock
      .mockResolvedValueOnce(
        okResponse([
          ['text', { delta: 'partial' }],
          ['error', { code: 'provider_error', message: 'The model is unavailable.' }],
        ]),
      )
      .mockResolvedValueOnce(okResponse([['text', { delta: 'Second try worked.' }], DONE]));
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'What failed?');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The model returned an error');
    expect(alert).toHaveTextContent('The model is unavailable.');
    expect(screen.getByText('partial')).toBeInTheDocument();

    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    await screen.findByText('Second try worked.');
    expect(sentBody(1).message).toBe('What failed?');
    // No thread was started, so there is nothing stored to reuse.
    expect(sentBody(1).retry).toBeUndefined();
    // The failed exchange is replaced, not duplicated.
    expect(screen.getAllByText('What failed?')).toHaveLength(1);
    expect(screen.queryByText('partial')).not.toBeInTheDocument();
  });

  it('Retry in a started thread asks the server to reuse the stored question', async () => {
    fetchMock
      .mockResolvedValueOnce(
        okResponse([
          ['conversation', { conversation_id: 'conv_r' }],
          ['error', { code: 'provider_error', message: 'The model is unavailable.' }],
        ]),
      )
      .mockResolvedValueOnce(okResponse([['text', { delta: 'Second try worked.' }], DONE]))
      .mockResolvedValueOnce(okResponse([['text', { delta: 'Follow-up answer.' }], DONE]));
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'What failed?');
    expect(sentBody(0).retry).toBeUndefined();

    await user.click(within(await screen.findByRole('alert')).getByRole('button', { name: 'Retry' }));
    await screen.findByText('Second try worked.');
    expect(sentBody(1)).toMatchObject({ message: 'What failed?', conversation_id: 'conv_r', retry: true });

    // An ordinary follow-up is never marked as a retry.
    await ask(user, 'And then?');
    await screen.findByText('Follow-up answer.');
    expect(sentBody(2).retry).toBeUndefined();
  });

  it('shows the token usage from the done event under the answer', async () => {
    fetchMock.mockResolvedValue(
      okResponse([
        ['text', { delta: 'All good.' }],
        ['done', { stop_reason: 'stop', input_tokens: 1234, output_tokens: 260, usage: { input_tokens: 1234, output_tokens: 260 }, key_source: 'user' }],
      ]),
    );
    renderPanel();
    await ask(userEvent.setup(), 'hi');
    expect(await screen.findByText('1.2k in · 260 out')).toBeInTheDocument();
  });

  it('says the monthly budget is reached on 429 helper_budget_exceeded', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(429, {
        error: { code: 'helper_budget_exceeded', message: "This org's monthly session-helper token budget has been reached." },
      }),
    );
    renderPanel();
    await ask(userEvent.setup(), 'q');
    expect(await screen.findByText('Monthly budget reached')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open org settings' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('surfaces a transport error message', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, { error: { code: 'helper_error', message: 'boom' } }));
    renderPanel();
    await ask(userEvent.setup(), 'What failed?');
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
  });

  it('explains helper_busy and offers Retry', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(409, { detail: { code: 'helper_busy', message: 'You already have the maximum number of helper turns running.' } }),
    );
    renderPanel();
    await ask(userEvent.setup(), 'q');
    expect(await screen.findByText('The helper is busy')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('offers a new conversation on scope_mismatch', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(409, { detail: { code: 'scope_mismatch', message: 'This conversation belongs to a different scope.' } }),
    );
    renderPanel();
    const user = userEvent.setup();
    await ask(user, 'q');
    expect(await screen.findByText('This conversation has a different scope')).toBeInTheDocument();
    const alert = screen.getByRole('alert');
    await user.click(within(alert).getByRole('button', { name: 'New conversation' }));
    expect(screen.queryByText('This conversation has a different scope')).not.toBeInTheDocument();
  });

  it('points at settings when the key disappears mid-conversation', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(409, { detail: { code: 'helper_key_required', message: 'No LLM key is configured.' } }),
    );
    renderPanel();
    await ask(userEvent.setup(), 'q');
    expect(await screen.findByText('No model key available')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open settings' })).toHaveAttribute('href', '/settings/data-protection');
    expect(statusRefetchMock).toHaveBeenCalled();
  });

  it('reports a network failure', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    renderPanel();
    await ask(userEvent.setup(), 'q');
    expect(await screen.findByText('Connection problem')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('flags a stream that ends without a done event', async () => {
    fetchMock.mockResolvedValue(okResponse([['text', { delta: 'half an ans' }]]));
    renderPanel();
    await ask(userEvent.setup(), 'q');
    expect(await screen.findByText('The answer was cut off')).toBeInTheDocument();
    expect(screen.getByText('half an ans')).toBeInTheDocument();
  });
});

describe('HelperPanel — gate states', () => {
  it('shows the Enterprise upsell when the helper is disabled', () => {
    setStatus({ enabled: false, key_source: null });
    renderPanel();
    expect(screen.getByRole('heading', { name: 'An Enterprise feature' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View plans' })).toHaveAttribute('href', '/settings/billing');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    // The gate notice lives in the body; header controls stay available so the
    // user can switch scope or browse history.
    expect(screen.getByRole('button', { name: 'New conversation' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close helper' })).toBeInTheDocument();
  });

  it('prompts for a key with a settings button, adding an org-admin hint for members', async () => {
    const onClose = vi.fn();
    setStatus({ enabled: true, key_source: null });
    useMeMock.mockReturnValue({ data: { org_id: 'org_1', org_role: 'member', email: 'm@x.test', tier: 'team' } });
    renderPanel(undefined, onClose);

    expect(screen.getByRole('heading', { name: 'Connect a model to get started' })).toBeInTheDocument();
    expect(screen.getByText(/Settings → Data protection → Security review model/)).toBeInTheDocument();
    expect(screen.getByText('Or ask your org admin to add an org key.')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Set an org key' })).not.toBeInTheDocument();
    const cta = screen.getByRole('link', { name: 'Open Data protection settings' });
    expect(cta).toHaveAttribute('href', '/settings/data-protection');
    await userEvent.setup().click(cta);
    expect(onClose).toHaveBeenCalled();
  });

  it('offers org admins the org key setting', () => {
    setStatus({ enabled: true, key_source: null });
    renderPanel();
    expect(screen.getByRole('link', { name: 'Set an org key' })).toHaveAttribute(
      'href',
      '/settings/organization?tab=policies',
    );
    expect(screen.queryByText('Or ask your org admin to add an org key.')).not.toBeInTheDocument();
  });

  it('omits the org hints for users without an org', () => {
    setStatus({ enabled: true, key_source: null });
    useMeMock.mockReturnValue({ data: { email: 'solo@x.test', tier: 'free' } });
    renderPanel();
    expect(screen.queryByText(/org admin/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Set an org key' })).not.toBeInTheDocument();
  });

  it('offers Retry when the status check fails', async () => {
    useHelperStatusMock.mockReturnValue({ data: undefined, isLoading: false, isError: true, refetch: statusRefetchMock });
    renderPanel();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }));
    expect(statusRefetchMock).toHaveBeenCalled();
  });
});

describe('HelperPanel — scope', () => {
  it('requests helper status for the project when opened scoped to a project', () => {
    useHelperStatusMock.mockClear();
    renderPanel({ project_id: 'proj_1', label: 'Acme Widgets' });
    expect(useHelperStatusMock).toHaveBeenCalledWith(true, { project_id: 'proj_1', label: 'Acme Widgets' });
  });

  it('requests helper status for the session when opened scoped to sessions', () => {
    useHelperStatusMock.mockClear();
    renderPanel({ session_ids: ['ses_1'], label: 'Fix the login bug' });
    expect(useHelperStatusMock).toHaveBeenCalledWith(true, { session_ids: ['ses_1'], label: 'Fix the login bug' });
  });

  it('requests helper status for the new scope after a scope switch', async () => {
    useHelperStatusMock.mockClear();
    fetchMock.mockResolvedValue(okResponse([['text', { delta: 'hi' }], DONE]));
    renderPanel({ session_ids: ['ses_1'], label: 'Fix the login bug' });
    expect(useHelperStatusMock).toHaveBeenCalledWith(true, { session_ids: ['ses_1'], label: 'Fix the login bug' });

    const user = userEvent.setup();
    await ask(user, 'one');
    await screen.findByText('hi');

    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    const menu = screen.getByRole('menu', { name: 'Ask about' });
    await user.click(within(menu).getByRole('menuitemradio', { name: /All my sessions/ }));

    expect(useHelperStatusMock).toHaveBeenLastCalledWith(true, undefined);
  });

  it('shows the entry scope on the chip and sends it on a fresh conversation', async () => {
    fetchMock.mockResolvedValue(okResponse([['text', { delta: 'hi' }], DONE]));
    renderPanel({ project_id: 'proj_1', label: 'Acme Widgets' });
    expect(screen.getByRole('button', { name: 'Scope: Acme Widgets. Change scope' })).toBeInTheDocument();
    expect(composer()).toHaveAttribute('placeholder', 'Ask about Acme Widgets…');
    await ask(userEvent.setup(), 'summarize');
    await screen.findByText('hi');
    // The label is display-only; only ids reach the API.
    expect(sentBody().scope).toEqual({ project_id: 'proj_1' });
  });

  it('switches to all sessions from the scope menu, starting a new conversation', async () => {
    fetchMock
      .mockResolvedValueOnce(okResponse([['conversation', { conversation_id: 'c1' }], ['text', { delta: 'first' }], DONE]))
      .mockResolvedValueOnce(okResponse([['text', { delta: 'second' }], DONE]));
    renderPanel({ session_ids: ['ses_1'] });
    const user = userEvent.setup();
    await ask(user, 'one');
    await screen.findByText('first');

    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    const menu = screen.getByRole('menu', { name: 'Ask about' });
    expect(within(menu).getByRole('menuitemradio', { name: /This session/ })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('Switching scope starts a new conversation.')).toBeInTheDocument();
    await user.click(within(menu).getByRole('menuitemradio', { name: /All my sessions/ }));

    expect(screen.queryByText('first')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Scope: All my sessions. Change scope' })).toBeInTheDocument();
    await ask(user, 'two');
    await screen.findByText('second');
    expect(sentBody(1)).toEqual({ message: 'two', stream: true });
  });

  it('Escape closes the scope menu without closing the drawer', async () => {
    const onClose = vi.fn();
    renderPanel({ project_id: 'proj_1' }, onClose);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Change scope/ })).toHaveFocus();
  });

  it('does not leak a stale conversation id after the drawer is reset mid-turn', async () => {
    fetchMock.mockImplementation(
      hangAfter([
        ['conversation', { conversation_id: 'conv_A' }],
        ['text', { delta: 'partial' }],
      ]),
    );
    const { rerender } = render(
      <MemoryRouter>
        <HelperPanel open onClose={vi.fn()} scope={{ session_ids: ['ses_1'] }} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await ask(user, 'first');
    await screen.findByText('partial');

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
    await screen.findByRole('button', { name: 'Send' });

    await ask(user, 'second');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = sentBody(1);
    expect(body.conversation_id).toBeUndefined();
    expect(body.scope).toEqual({ project_id: 'proj_1' });
  });
});

describe('HelperPanel — scope status transitions', () => {
  it('keeps history and the scope menu when switching to a disabled scope, and switching back restores chat', async () => {
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope ? statusResult({ enabled: true, key_source: 'user' }) : statusResult({ enabled: false, key_source: null }),
    );
    renderPanel({ session_ids: ['ses_1'] });
    expect(composer()).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    await user.click(within(screen.getByRole('menu', { name: 'Ask about' })).getByRole('menuitemradio', { name: /All my sessions/ }));

    // The disabled scope shows the Enterprise notice in the body, but the
    // header controls stay available so the user can switch back.
    expect(screen.getByRole('heading', { name: 'An Enterprise feature' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Conversation history' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Scope: All my sessions. Change scope' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'New conversation' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    await user.click(within(screen.getByRole('menu', { name: 'Ask about' })).getByRole('menuitemradio', { name: /This session/ }));

    expect(composer()).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'An Enterprise feature' })).not.toBeInTheDocument();
  });

  it('explains a lost scope after restoring a thread instead of looping Retry', async () => {
    setConversations([{ id: 'c1', title: 'Gone project thread' }]);
    getHelperConversationMock.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'Gone project thread',
      created_at: '',
      updated_at: '',
      scope: { project_id: 'gone', session_ids: [] },
      messages: [
        { id: 'm1', role: 'user', content: 'question?' },
        { id: 'm2', role: 'assistant', content: 'the answer' },
      ],
    });
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope?.project_id === 'gone'
        ? statusResult(undefined, { isError: true, error: new ApiError(404, JSON.stringify({ detail: 'Project not found' })) })
        : statusResult({ enabled: true, key_source: 'user' }),
    );
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Conversation history' }));
    await user.click(screen.getByRole('button', { name: /^Gone project thread/ }));

    expect(await screen.findByText("You no longer have access to this conversation's scope.", { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Change scope/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Conversation history' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start a new conversation' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('shows the server message when a restored scope spans multiple orgs', async () => {
    setConversations([{ id: 'c1', title: 'Mixed org thread' }]);
    getHelperConversationMock.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'Mixed org thread',
      created_at: '',
      updated_at: '',
      scope: { project_id: null, session_ids: ['ses_a', 'ses_b'] },
      messages: [
        { id: 'm1', role: 'user', content: 'question?' },
        { id: 'm2', role: 'assistant', content: 'the answer' },
      ],
    });
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope && !scope.project_id && (scope.session_ids?.length ?? 0) > 1
        ? statusResult(undefined, {
            isError: true,
            error: new ApiError(
              400,
              JSON.stringify({ detail: { code: 'scope_mixed_orgs', message: 'The selected sessions belong to different organizations.' } }),
            ),
          })
        : statusResult({ enabled: true, key_source: 'user' }),
    );
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Conversation history' }));
    await user.click(screen.getByRole('button', { name: /^Mixed org thread/ }));

    expect(await screen.findByText('The selected sessions belong to different organizations.', { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start a new conversation' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('keeps the composer mounted with its draft and disables Send while a scope switch rechecks status', async () => {
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope
        ? statusResult({ enabled: true, key_source: 'user' })
        : statusResult({ enabled: true, key_source: 'user' }, { isPlaceholderData: true }),
    );
    renderPanel({ session_ids: ['ses_1'] });
    const user = userEvent.setup();
    const box = composer();
    await user.type(box, 'draft');
    expect(box).toHaveValue('draft');

    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    await user.click(within(screen.getByRole('menu', { name: 'Ask about' })).getByRole('menuitemradio', { name: /All my sessions/ }));

    // The same textarea node stays mounted with the draft intact...
    expect(composer()).toBe(box);
    expect(box).toHaveValue('draft');
    // ...but Send is disabled and the composer reports the recheck.
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(screen.getByText('Checking access…')).toBeInTheDocument();
  });

  it('keeps restored turns visible while their scope status is rechecked', async () => {
    setConversations([{ id: 'c1', title: 'Project thread' }]);
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
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope?.project_id === 'proj_1'
        ? statusResult({ enabled: true, key_source: 'user' }, { isPlaceholderData: true })
        : statusResult({ enabled: true, key_source: 'user' }),
    );
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Conversation history' }));
    await user.click(screen.getByRole('button', { name: /^Project thread/ }));

    // The restored turns stay on screen while the scope's status is rechecked.
    expect(await screen.findByText('the answer')).toBeInTheDocument();
    expect(screen.getByText('question?')).toBeInTheDocument();
    expect(screen.queryByLabelText('Loading the helper')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
  });

  it('disables the suggested prompts while a scope switch rechecks status', async () => {
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope
        ? statusResult({ enabled: true, key_source: 'user' })
        : statusResult({ enabled: true, key_source: 'user' }, { isPlaceholderData: true }),
    );
    renderPanel({ project_id: 'proj_1' });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Change scope/ }));
    await user.click(within(screen.getByRole('menu', { name: 'Ask about' })).getByRole('menuitemradio', { name: /All my sessions/ }));

    const list = screen.getByText('Try asking').nextElementSibling as HTMLElement;
    const chips = within(list).getAllByRole('button');
    expect(chips.length).toBeGreaterThan(0);
    for (const chip of chips) expect(chip).toBeDisabled();
    await user.click(chips[0]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('offers all sessions when the entry scope itself is unavailable, and announces why', async () => {
    useHelperStatusMock.mockImplementation((_open: boolean, scope?: HelperScope) =>
      scope?.project_id === 'gone'
        ? statusResult(undefined, { isError: true, error: new ApiError(404, JSON.stringify({ detail: 'Project not found' })) })
        : statusResult({ enabled: true, key_source: 'user' }),
    );
    renderPanel({ project_id: 'gone' });
    const user = userEvent.setup();

    const message = "You no longer have access to this conversation's scope.";
    expect(await screen.findByText(message, { selector: 'p' })).toBeInTheDocument();
    const regions = screen.getAllByRole('status');
    expect(regions.some((r) => r.textContent?.includes(message))).toBe(true);
    expect(screen.queryByRole('button', { name: 'Start a new conversation' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Ask about all my sessions' }));
    expect(useHelperStatusMock).toHaveBeenLastCalledWith(true, undefined);
    expect(composer()).toBeInTheDocument();
  });
});

describe('HelperPanel — history', () => {
  it('lists recent conversations in the empty state and resumes one', async () => {
    setConversations([{ id: 'c1', title: 'First thread' }]);
    getHelperConversationMock.mockResolvedValue({
      id: 'c1',
      org_id: null,
      title: 'First thread',
      created_at: '',
      updated_at: '',
      scope: null,
      messages: [
        { id: 'm1', role: 'user', content: 'question?' },
        { id: 't1', role: 'tool', content: 'raw tool output', tool_name: 'search_sessions', tool_call_id: 'tc1', tool_args: { query: 'x' } },
        { id: 'm2', role: 'assistant', content: 'the answer [session:ses_1]', citations: [{ session_id: 'ses_1', message_index: null, title: 'S1' }] },
      ],
    });
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /First thread/ }));

    expect(await screen.findByText(/the answer/)).toBeInTheDocument();
    expect(getHelperConversationMock).toHaveBeenCalledWith('c1');
    expect(screen.getByText('question?')).toBeInTheDocument();
    // Stored tool rows fold into the answer as a step; their raw output stays hidden.
    expect(screen.getByRole('button', { name: /Searched sessions for “x”/ })).toBeInTheDocument();
    expect(screen.queryByText('raw tool output')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Source 1: S1' })).toHaveAttribute('href', '/sessions/ses_1');
  });

  it('opens the history view, groups threads and resumes the chosen one with its scope', async () => {
    setConversations([
      { id: 'c1', org_id: 'org_1', title: 'Project thread' },
      { id: 'c2', title: null, updated_at: '2020-01-01T00:00:00Z' },
    ]);
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
    fetchMock.mockResolvedValue(okResponse([['conversation', { conversation_id: 'c1' }], ['text', { delta: 'again' }], DONE]));
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Conversation history' }));
    const nav = screen.getByRole('navigation', { name: 'Conversation history' });
    expect(within(nav).getByRole('heading', { name: 'Today' })).toBeInTheDocument();
    expect(within(nav).getByRole('heading', { name: 'Older' })).toBeInTheDocument();
    expect(within(nav).getByText('Acme')).toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: /^Untitled conversation/ })).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();

    await user.click(within(nav).getByRole('button', { name: /^Project thread/ }));
    await screen.findByText('the answer');
    // The restored scope is shown and re-sent on the follow-up question.
    expect(screen.getByRole('button', { name: /Scope: This project/ })).toBeInTheDocument();
    await ask(user, 'again?');
    await screen.findByText('again');
    const body = sentBody();
    expect(body.conversation_id).toBe('c1');
    expect(body.scope).toEqual({ project_id: 'proj_1', session_ids: [] });
  });

  it('shows an empty history state', async () => {
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Conversation history' }));
    expect(screen.getByText('No conversations yet')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Ask a question' }));
    expect(composer()).toBeInTheDocument();
  });

  it('deletes a conversation after confirming; Escape closes only the dialog', async () => {
    setConversations([{ id: 'c1', title: 'Doomed' }]);
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Conversation history' }));

    await user.click(screen.getByRole('button', { name: 'Delete Doomed' }));
    expect(await screen.findByText(/Delete conversation\?/i)).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText(/Delete conversation\?/i)).not.toBeInTheDocument());
    // The drawer stays open — only the nested dialog was dismissed.
    expect(screen.getByRole('heading', { name: 'Session helper' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Delete Doomed' }));
    expect(screen.getByText(/permanently deletes/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    expect(deleteMutateMock).toHaveBeenCalledWith('c1', expect.any(Object));
  });
});
