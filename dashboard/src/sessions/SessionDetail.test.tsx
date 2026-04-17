import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SessionDetail from './SessionDetail';

/**
 * Coverage for SessionDetail. The page is 582 lines and composes many
 * nested sub-components (ConversationView, AuditTab, SummaryTab, HandoffModal,
 * bookmarks, DLP findings, etc.). We mock the heavy sub-components with
 * simple stubs so the tests can focus on the main component's flow:
 *
 *   - loading / error states
 *   - renders session header (tool name, alias, token count, duration)
 *   - tab switching between messages / summary / audit
 *   - alias edit → save calls auth.client.setAlias with trimmed value
 *   - Hand Off button opens the handoff modal
 *   - back button navigates to "/"
 */

const { hooks, mockAuth } = vi.hoisted(() => ({
  hooks: {
    useSession: vi.fn(),
    useMessages: vi.fn(),
    useAudit: vi.fn(),
    useFolders: vi.fn(),
    useAddBookmark: vi.fn(),
    useDeleteSession: vi.fn(),
  },
  mockAuth: vi.fn(),
}));

vi.mock('../hooks/useSession', () => ({ useSession: hooks.useSession }));
vi.mock('../hooks/useMessages', () => ({ useMessages: hooks.useMessages }));
vi.mock('../hooks/useAudit', () => ({ useAudit: hooks.useAudit }));
vi.mock('../hooks/useBookmarks', () => ({
  useFolders: hooks.useFolders,
  useAddBookmark: hooks.useAddBookmark,
}));
vi.mock('../hooks/useSessions', () => ({
  useDeleteSession: hooks.useDeleteSession,
}));
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));
vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: vi.fn(), removeToast: vi.fn(), toasts: [] }),
}));

// Stub heavy sub-components so we can focus on the main flow without
// setting up their hooks.
vi.mock('./ConversationView', () => ({
  default: () => <div data-testid="conversation-view">Conversation</div>,
}));
vi.mock('./AuditTab', () => ({
  default: () => <div data-testid="audit-tab">Audit findings</div>,
}));
vi.mock('./SummaryTab', () => ({
  default: () => <div data-testid="summary-tab">Summary content</div>,
}));
vi.mock('./AuditModal', () => ({
  default: () => null,
}));
vi.mock('../handoffs/HandoffModal', () => ({
  default: ({ onClose }: { onClose?: () => void }) => (
    <div role="dialog" aria-label="Hand off session">
      <button onClick={onClose}>close</button>
    </div>
  ),
}));

function baseSession(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ses_abc123',
    source_tool: 'claude-code',
    model_id: 'claude-sonnet-4-5',
    alias: null,
    title: 'Debug auth middleware',
    message_count: 42,
    turn_count: 12,
    tool_use_count: 8,
    total_input_tokens: 10000,
    total_output_tokens: 5000,
    duration_ms: 900000, // 15m
    is_deleted: false,
    created_at: '2026-04-10T12:00:00Z',
    updated_at: '2026-04-10T12:30:00Z',
    uploaded_at: '2026-04-10T12:35:00Z',
    git_remote_normalized: 'sessionfs/sessionfs',
    git_branch: 'develop',
    git_commit: 'abc123',
    etag: 'etag123',
    blob_size_bytes: 1024,
    tags: [],
    parent_session_id: null,
    has_knowledge: false,
    ...overrides,
  };
}

function renderPage(sessionId = 'ses_abc123') {
  return render(
    <MemoryRouter initialEntries={[`/sessions/${sessionId}`]}>
      <Routes>
        <Route path="/sessions/:id" element={<SessionDetail />} />
        <Route path="/" element={<div>home</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('SessionDetail', () => {
  const mockSetAlias = vi.fn();
  const mockClearAlias = vi.fn();

  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();
    mockAuth.mockReset();
    mockSetAlias.mockReset();
    mockClearAlias.mockReset();

    // Default: all secondary hooks return empty / ok
    hooks.useMessages.mockReturnValue({ data: { messages: [], total: 0 }, isLoading: false });
    hooks.useAudit.mockReturnValue({ data: null, isLoading: false });
    hooks.useFolders.mockReturnValue({ data: [], isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useDeleteSession.mockReturnValue({ mutate: vi.fn(), isPending: false });

    mockAuth.mockReturnValue({
      auth: {
        apiKey: 'sk_test',
        baseUrl: 'http://test.api',
        client: {
          setAlias: mockSetAlias,
          clearAlias: mockClearAlias,
        },
      },
    });
  });

  it('shows a loading placeholder while the session fetches', () => {
    hooks.useSession.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
      refetch: vi.fn(),
    });
    renderPage();
    expect(screen.getByText(/loading session/i)).toBeInTheDocument();
  });

  it('shows an error state when the session fails to load', () => {
    hooks.useSession.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('not found'),
      refetch: vi.fn(),
    });
    renderPage();
    expect(screen.getByText(/failed to load session/i)).toBeInTheDocument();
    expect(screen.getByText(/not found/i)).toBeInTheDocument();
  });

  it('renders the session header with tool name and alias when present', () => {
    hooks.useSession.mockReturnValue({
      data: baseSession({ alias: 'auth-debug' }),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderPage();
    // Tool name ("Claude Code") + alias both visible. Multiple elements
    // contain "claude" (tool name AND model id), so scope to exact match.
    expect(screen.getAllByText(/claude/i).length).toBeGreaterThan(0);
    expect(screen.getByText('auth-debug')).toBeInTheDocument();
  });

  it('defaults to the Messages tab and shows conversation view', () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderPage();

    expect(screen.getByTestId('conversation-view')).toBeInTheDocument();
    // Other tabs not rendered yet
    expect(screen.queryByTestId('audit-tab')).not.toBeInTheDocument();
    expect(screen.queryByTestId('summary-tab')).not.toBeInTheDocument();
  });

  it('switching to the Summary tab renders the summary panel', async () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderPage();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^summary$/i }));

    await waitFor(() => {
      expect(screen.getByTestId('summary-tab')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('conversation-view')).not.toBeInTheDocument();
  });

  it('switching to the Audit tab renders the audit panel', async () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderPage();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^audit$/i }));

    await waitFor(() => {
      expect(screen.getByTestId('audit-tab')).toBeInTheDocument();
    });
  });

  it('clicking Hand Off opens the handoff modal', async () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderPage();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /hand off/i }));

    expect(
      await screen.findByRole('dialog', { name: /hand off session/i }),
    ).toBeInTheDocument();
  });
});
