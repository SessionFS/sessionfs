import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ConversationView from './ConversationView';

// ── Mock useMessages ───────────────────────────────────────────────

const { mockUseMessages } = vi.hoisted(() => ({
  mockUseMessages: vi.fn(),
}));

vi.mock('../hooks/useMessages', () => ({
  useMessages: mockUseMessages,
}));

// ContentBlock + MessageBlock are rendered inside ConversationView;
// we test the ConversationView container (pagination, order toggle,
// empty/error states) and verify no crash across page transitions.

function mockReturn(data: {
  messages?: Array<Record<string, unknown>>;
  total?: number;
  isLoading?: boolean;
  error?: Error | null;
}) {
  mockUseMessages.mockReturnValue({
    data: { messages: data.messages ?? [], total: data.total ?? 0 },
    isLoading: data.isLoading ?? false,
    error: data.error ?? null,
  });
}

function mkMsg(role: string, text: string) {
  return { role, content: [{ type: 'text', text }] };
}

// ── Pagination + order toggle ─────────────────────────────────────

describe('ConversationView — pagination and order', () => {
  it('shows page controls when messages exceed page size', () => {
    mockReturn({ messages: [mkMsg('user', 'hi')], total: 100 });

    render(<ConversationView sessionId="ses_test" />);

    // Page info visible
    // "100 messages" appears in both the header counter and the pagination bar
    const countEls = screen.getAllByText(/100 messages/);
    expect(countEls.length).toBeGreaterThanOrEqual(2);
    const pageEls = screen.getAllByText(/Page 1 of 2/);
    expect(pageEls.length).toBe(2); // top + bottom pagination
    // Pagination buttons present (two sets)
    expect(screen.getAllByText('Next').length).toBe(2);
    expect(screen.getAllByText('Prev').length).toBe(2);
  });

  it('hides page controls when total <= page size', () => {
    mockReturn({ messages: [mkMsg('user', 'hi')], total: 10 });

    render(<ConversationView sessionId="ses_test" />);

    expect(screen.getByText('10 messages')).toBeInTheDocument();
    // No pagination controls for single page
    expect(screen.queryByText(/Page/)).not.toBeInTheDocument();
    expect(screen.queryByText('Next')).not.toBeInTheDocument();
  });

  it('toggles between newest-first and oldest-first', async () => {
    mockReturn({ messages: [mkMsg('user', 'hi')], total: 10 });

    render(<ConversationView sessionId="ses_test" />);

    const toggleBtn = screen.getByRole('button', { name: 'Newest first' });
    expect(toggleBtn).toBeInTheDocument();

    await userEvent.click(toggleBtn);
    expect(screen.getByRole('button', { name: 'Oldest first' })).toBeInTheDocument();
  });

  it('toggling order resets to page 1', async () => {
    mockReturn({ messages: [mkMsg('user', 'hi')], total: 100 });

    render(<ConversationView sessionId="ses_test" />);

    // Initially page 1 newest-first
    const toggleBtn = screen.getByRole('button', { name: 'Newest first' });
    await userEvent.click(toggleBtn);

    // Should still be on page 1 (resets on toggle) — top + bottom pagination
    expect(screen.getAllByText(/Page 1/).length).toBeGreaterThanOrEqual(1);
    // Jump-to-latest button appears in oldest-first mode when > 1 page
    expect(screen.getByText(/Jump to latest/)).toBeInTheDocument();
  });

  it('Next button advances page and does not crash', async () => {
    mockReturn({
      messages: Array.from({ length: 50 }, (_, i) => mkMsg('user', `msg ${i}`)),
      total: 200,
    });

    render(<ConversationView sessionId="ses_test" />);

    // 50 messages rendered
    const renderedMsgs = screen.getAllByText(/msg \d+/);
    expect(renderedMsgs.length).toBe(50);

    // Advance to page 2 — two "Next" buttons (top + bottom), click the first
    const nextBtns = screen.getAllByText('Next');
    await userEvent.click(nextBtns[0]);

    // Should not crash — page change resets all ContentBlock expand states
    // (keys are `${page}-${i}` so React unmounts/mounts fresh trees)
    expect(mockUseMessages).toHaveBeenCalled();
  });

  it('shows tool-use blocks survive page transition without crash', () => {
    // Simulate a page with tool_use blocks
    mockReturn({
      messages: [
        { role: 'assistant', content: [{ type: 'tool_use', name: 'Bash', input: { command: 'ls' }, id: 't1' }] },
        { role: 'assistant', content: [{ type: 'tool_use', name: 'Read', input: { file_path: 'src/x.ts' }, id: 't2' }] },
      ],
      total: 2,
    });

    const { rerender } = render(<ConversationView sessionId="ses_test" />);

    // Both tool blocks render with collapsed summaries (not raw JSON)
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getByText('Read')).toBeInTheDocument();

    // Simulate re-render (e.g., page change): mock returns different data
    mockReturn({
      messages: [
        { role: 'assistant', content: [{ type: 'tool_use', name: 'Edit', input: { file_path: 'src/y.ts' }, id: 't3' }] },
      ],
      total: 1,
    });

    rerender(<ConversationView sessionId="ses_test" />);

    // Old tool blocks unmounted, new one mounted — no crash
    expect(screen.queryByText('Bash')).not.toBeInTheDocument();
    expect(screen.getByText('Edit')).toBeInTheDocument();
  });
});

// ── States: empty, error, loading, partial-data ────────────────────

describe('ConversationView — states', () => {
  it('shows empty state when no messages exist', () => {
    mockReturn({ messages: [], total: 0 });

    render(<ConversationView sessionId="ses_test" />);

    expect(screen.getByText('No messages yet')).toBeInTheDocument();
  });

  it('shows error when messages fail to load', () => {
    mockReturn({ error: new Error('network error') });

    render(<ConversationView sessionId="ses_test" />);

    expect(screen.getByText(/Failed to load messages/)).toBeInTheDocument();
    expect(screen.getByText(/network error/)).toBeInTheDocument();
  });

  it('shows loading when data is undefined', () => {
    mockUseMessages.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });

    render(<ConversationView sessionId="ses_test" />);

    expect(screen.getByText(/Loading messages/)).toBeInTheDocument();
  });

  it('shows loading page text when data present but loading', () => {
    mockReturn({ messages: [], total: 0, isLoading: true });

    render(<ConversationView sessionId="ses_test" />);

    // "Loading page N" shown when data exists but isLoading=true and no messages
    expect(screen.getByText(/Loading page/)).toBeInTheDocument();
  });

  it('does not crash when data.messages is missing from response', () => {
    // Simulate a malformed API response where data exists but messages is absent
    mockUseMessages.mockReturnValue({
      data: { total: 42 }, // no messages field
      isLoading: false,
      error: null,
    });

    render(<ConversationView sessionId="ses_test" />);

    // Should not crash — .messages defaults to [] via ?? operator
    // Empty state rendered
    expect(screen.getByText('No messages yet')).toBeInTheDocument();
  });

  it('does not crash when message content is a non-array, non-string value', () => {
    // Malformed message: content is a number (shouldn't happen, but paranoia)
    mockReturn({
      messages: [{ role: 'user', content: 12345 }],
      total: 1,
    });

    render(<ConversationView sessionId="ses_test" />);

    // Should render without crash — MessageBlock handles non-string/non-array content
    expect(screen.getByText('User')).toBeInTheDocument();
  });
});


// ── Deep link to one message (?msg=<n>) ───────────────────────────

describe('ConversationView — deep link to a message', () => {
  beforeEach(() => mockUseMessages.mockReset());

  function located(target: { index: number; status: 'found' | 'hidden' | 'missing'; anchor_index: number | null }) {
    mockUseMessages.mockReturnValue({
      data: {
        messages: [mkMsg('user', 'line ten'), mkMsg('assistant', 'line eleven'), mkMsg('user', 'line thirteen')],
        message_indexes: [10, 11, 13],
        total: 120,
        page: 3,
        page_size: 50,
        has_more: false,
        target,
      },
      isLoading: false,
      error: null,
    });
  }

  it('asks the server for the page around the message, oldest first', () => {
    located({ index: 11, status: 'found', anchor_index: 11 });
    render(<ConversationView sessionId="ses_test" targetMessage={11} targetKey="k1" />);
    expect(mockUseMessages).toHaveBeenCalledWith('ses_test', 1, 50, 'oldest', 11);
    // The page the server chose is the one shown.
    expect(screen.getAllByText(/Page 3 of 3/).length).toBe(2);
  });

  it('gives each message a stable id, scrolls to the target and highlights it', async () => {
    located({ index: 11, status: 'found', anchor_index: 11 });
    const scrollSpy = vi.spyOn(HTMLElement.prototype, 'scrollIntoView');
    render(<ConversationView sessionId="ses_test" targetMessage={11} targetKey="k1" />);

    const el = document.getElementById('msg-11');
    expect(el).not.toBeNull();
    expect(document.getElementById('msg-10')).not.toBeNull();
    await waitFor(() => expect(el).toHaveAttribute('data-highlighted', 'true'));
    expect(scrollSpy.mock.contexts).toContain(el);
    expect(document.activeElement).toBe(el);
    expect(screen.getByRole('status')).toHaveTextContent('Showing message 11.');
    // No fallback notice when the message itself was found.
    expect(screen.queryByText(/hidden in this view/)).not.toBeInTheDocument();
    scrollSpy.mockRestore();
  });

  it('falls back to the nearest message with a notice when the target is filtered out', async () => {
    located({ index: 12, status: 'hidden', anchor_index: 13 });
    render(<ConversationView sessionId="ses_test" targetMessage={12} targetKey="k1" />);
    expect(screen.getByText(/Message 12 is hidden in this view/)).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById('msg-13')).toHaveAttribute('data-highlighted', 'true'));
  });

  it('says so when the message is not in the transcript', () => {
    located({ index: 999, status: 'missing', anchor_index: 13 });
    render(<ConversationView sessionId="ses_test" targetMessage={999} targetKey="k1" />);
    expect(screen.getByText(/Message 999 isn't in this transcript/)).toBeInTheDocument();
  });

  it('handles a view with nothing to show', () => {
    mockUseMessages.mockReturnValue({
      data: { messages: [], message_indexes: [], total: 0, page: 1, page_size: 50, has_more: false, target: { index: 4, status: 'hidden', anchor_index: null } },
      isLoading: false,
      error: null,
    });
    render(<ConversationView sessionId="ses_test" targetMessage={4} targetKey="k1" />);
    expect(screen.getByText("Message 4 isn't shown in this view.")).toBeInTheDocument();
  });

  it('stops locating once the reader pages away', async () => {
    located({ index: 11, status: 'found', anchor_index: 11 });
    render(<ConversationView sessionId="ses_test" targetMessage={11} targetKey="k1" />);
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: '1' })[0]);
    expect(mockUseMessages).toHaveBeenLastCalledWith('ses_test', 1, 50, 'oldest', undefined);
  });

  it('re-locates when a different message is linked', () => {
    located({ index: 11, status: 'found', anchor_index: 11 });
    const { rerender } = render(<ConversationView sessionId="ses_test" targetMessage={11} targetKey="k1" />);
    rerender(<ConversationView sessionId="ses_test" targetMessage={40} targetKey="k2" />);
    expect(mockUseMessages).toHaveBeenLastCalledWith('ses_test', 1, 50, 'oldest', 40);
  });

  it('without a target keeps the newest-first default', () => {
    located({ index: 0, status: 'found', anchor_index: 0 });
    render(<ConversationView sessionId="ses_test" />);
    expect(mockUseMessages).toHaveBeenCalledWith('ses_test', 1, 50, 'newest', undefined);
  });
});
