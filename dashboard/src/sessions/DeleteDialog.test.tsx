import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Tests for the delete lifecycle UX:
 *
 * 1. Delete dialog shows two scope options (cloud / everywhere)
 * 2. Selecting "Remove from cloud" calls deleteSession with scope=cloud
 * 3. Selecting "Delete everywhere" calls deleteSession with scope=everywhere
 * 4. Trash view renders deleted sessions
 * 5. Restore button calls restore endpoint
 * 6. Bulk delete applies same scope to all selected
 */

// ---- Mocks ----

const { hooks, mockAuth, mockToast } = vi.hoisted(() => ({
  hooks: {
    useSession: vi.fn(),
    useMessages: vi.fn(),
    useAudit: vi.fn(),
    useFolders: vi.fn(),
    useAddBookmark: vi.fn(),
    useFolderSessions: vi.fn(),
    useSessions: vi.fn(),
    useDeletedSessions: vi.fn(),
    useRestoreSession: vi.fn(),
    useDeleteSession: vi.fn(),
  },
  mockAuth: vi.fn(),
  mockToast: { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] },
}));

vi.mock('../hooks/useSession', () => ({ useSession: hooks.useSession }));
vi.mock('../hooks/useMessages', () => ({ useMessages: hooks.useMessages }));
vi.mock('../hooks/useAudit', () => ({ useAudit: hooks.useAudit }));
vi.mock('../hooks/useBookmarks', () => ({
  useFolders: hooks.useFolders,
  useAddBookmark: hooks.useAddBookmark,
  useFolderSessions: hooks.useFolderSessions,
}));
vi.mock('../hooks/useSessions', () => ({
  useSessions: hooks.useSessions,
  useDeletedSessions: hooks.useDeletedSessions,
  useRestoreSession: hooks.useRestoreSession,
  useDeleteSession: hooks.useDeleteSession,
}));
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));
vi.mock('../hooks/useToast', () => ({
  useToast: () => mockToast,
}));

// Stub heavy sub-components
vi.mock('./ConversationView', () => ({
  default: () => <div data-testid="conversation-view">Conversation</div>,
}));
vi.mock('./AuditTab', () => ({
  default: () => <div data-testid="audit-tab">Audit</div>,
}));
vi.mock('./SummaryTab', () => ({
  default: () => <div data-testid="summary-tab">Summary</div>,
}));
vi.mock('./AuditModal', () => ({
  default: () => null,
}));
vi.mock('../handoffs/HandoffModal', () => ({
  default: () => null,
}));
vi.mock('../components/BookmarkSidebar', () => ({
  default: () => <div data-testid="sidebar" />,
  MobileNavChips: () => null,
}));

// ---- Helpers ----

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
    duration_ms: 900000,
    blob_size_bytes: 1024,
    etag: 'etag123',
    created_at: '2026-04-10T12:00:00Z',
    updated_at: '2026-04-10T12:30:00Z',
    uploaded_at: '2026-04-10T12:35:00Z',
    tags: [],
    parent_session_id: null,
    ...overrides,
  };
}

function deletedSession(overrides: Record<string, unknown> = {}) {
  return baseSession({
    is_deleted: true,
    deleted_at: '2026-04-12T10:00:00Z',
    deleted_by: 'user_1',
    delete_scope: 'cloud',
    purge_after: '2026-05-12T10:00:00Z',
    title: 'Deleted session',
    ...overrides,
  });
}

// ---- Tests for SessionDetail delete dialog ----

describe('SessionDetail delete dialog', () => {
  let mockDeleteMutate: ReturnType<typeof vi.fn>;
  let SessionDetail: typeof import('./SessionDetail').default;

  beforeEach(async () => {
    vi.resetModules();
    for (const h of Object.values(hooks)) h.mockReset();
    mockAuth.mockReset();
    mockToast.addToast.mockReset();

    mockDeleteMutate = vi.fn();

    hooks.useMessages.mockReturnValue({ data: { messages: [], total: 0 }, isLoading: false });
    hooks.useAudit.mockReturnValue({ data: null, isLoading: false });
    hooks.useFolders.mockReturnValue({ data: [], isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useDeleteSession.mockReturnValue({
      mutate: mockDeleteMutate,
      isPending: false,
    });

    mockAuth.mockReturnValue({
      auth: {
        apiKey: 'sk_test',
        baseUrl: 'http://test.api',
        client: {
          setAlias: vi.fn(),
          clearAlias: vi.fn(),
          deleteSession: vi.fn(),
        },
      },
    });

    const mod = await import('./SessionDetail');
    SessionDetail = mod.default;
  });

  function renderDetail() {
    return render(
      <MemoryRouter initialEntries={['/sessions/ses_abc123']}>
        <Routes>
          <Route path="/sessions/:id" element={<SessionDetail />} />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it('shows two scope options when delete dialog opens', async () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderDetail();
    const user = userEvent.setup();

    // Open kebab menu
    const moreBtn = screen.getAllByRole('button').find(
      (b) => b.querySelector('circle') !== null,
    )!;
    await user.click(moreBtn);

    // Click "Delete Session"
    await user.click(screen.getByText('Delete Session'));

    // Dialog should appear with two radio options
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByLabelText(/remove from cloud/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/delete everywhere/i)).toBeInTheDocument();
  });

  it('calls deleteSession with scope=cloud when "Remove from cloud" is selected', async () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderDetail();
    const user = userEvent.setup();

    // Open kebab → Delete Session → dialog
    const moreBtn = screen.getAllByRole('button').find(
      (b) => b.querySelector('circle') !== null,
    )!;
    await user.click(moreBtn);
    await user.click(screen.getByText('Delete Session'));

    // "Remove from cloud" is already selected by default
    // Click "Delete" confirm
    const deleteBtn = screen.getAllByRole('button', { name: /^delete$/i }).find(
      (b) => b.closest('[role="dialog"]'),
    )!;
    await user.click(deleteBtn);

    expect(mockDeleteMutate).toHaveBeenCalledWith(
      { id: 'ses_abc123', scope: 'cloud' },
      expect.any(Object),
    );
  });

  it('calls deleteSession with scope=everywhere when "Delete everywhere" is selected', async () => {
    hooks.useSession.mockReturnValue({
      data: baseSession(),
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    renderDetail();
    const user = userEvent.setup();

    const moreBtn = screen.getAllByRole('button').find(
      (b) => b.querySelector('circle') !== null,
    )!;
    await user.click(moreBtn);
    await user.click(screen.getByText('Delete Session'));

    // Select "Delete everywhere"
    await user.click(screen.getByLabelText(/delete everywhere/i));

    const deleteBtn = screen.getAllByRole('button', { name: /^delete$/i }).find(
      (b) => b.closest('[role="dialog"]'),
    )!;
    await user.click(deleteBtn);

    expect(mockDeleteMutate).toHaveBeenCalledWith(
      { id: 'ses_abc123', scope: 'everywhere' },
      expect.any(Object),
    );
  });
});

// ---- Tests for Trash view ----

describe('Trash view', () => {
  let SessionList: typeof import('./SessionList').default;
  let mockRestoreMutate: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    vi.resetModules();
    for (const h of Object.values(hooks)) h.mockReset();
    mockAuth.mockReset();
    mockToast.addToast.mockReset();

    mockRestoreMutate = vi.fn();

    hooks.useSessions.mockReturnValue({
      data: { sessions: [baseSession()], total: 1, page: 1, page_size: 20, has_more: false },
      isLoading: false,
      error: null,
    });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });
    hooks.useDeletedSessions.mockReturnValue({
      data: {
        sessions: [
          deletedSession({ id: 'ses_del1', title: 'Deleted session A' }),
          deletedSession({ id: 'ses_del2', title: 'Deleted session B', delete_scope: 'both' }),
        ],
        total: 2,
        page: 1,
        page_size: 20,
        has_more: false,
      },
      isLoading: false,
      error: null,
    });
    hooks.useRestoreSession.mockReturnValue({
      mutate: mockRestoreMutate,
      isPending: false,
    });

    mockAuth.mockReturnValue({
      auth: {
        apiKey: 'sk_test',
        baseUrl: 'http://test.api',
        client: { listInbox: vi.fn().mockResolvedValue({ handoffs: [], total: 0 }) },
      },
    });

    const mod = await import('./SessionList');
    SessionList = mod.default;
  });

  function renderList() {
    return render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <SessionList />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('renders deleted sessions when Trash toggle is clicked', async () => {
    renderList();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /trash/i }));

    await waitFor(() => {
      expect(screen.getByText('Deleted session A')).toBeInTheDocument();
      expect(screen.getByText('Deleted session B')).toBeInTheDocument();
    });

    // Scope badges
    expect(screen.getByText('cloud')).toBeInTheDocument();
    expect(screen.getByText('everywhere')).toBeInTheDocument();
  });

  it('calls restore endpoint when Restore is clicked', async () => {
    renderList();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /trash/i }));

    await waitFor(() => {
      expect(screen.getByText('Deleted session A')).toBeInTheDocument();
    });

    const restoreButtons = screen.getAllByRole('button', { name: /restore/i });
    await user.click(restoreButtons[0]);

    expect(mockRestoreMutate).toHaveBeenCalledWith('ses_del1', expect.any(Object));
  });

  it('shows generic toast when restore response has local_copy_may_be_missing=false', async () => {
    // Configure mutate to invoke onSuccess with cloud-only restore response
    mockRestoreMutate.mockImplementation((_id: string, opts: { onSuccess?: (data: Record<string, unknown>) => void }) => {
      opts.onSuccess?.({ local_copy_may_be_missing: false, restored_from_scope: 'cloud' });
    });

    renderList();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /trash/i }));
    await waitFor(() => {
      expect(screen.getByText('Deleted session A')).toBeInTheDocument();
    });

    const restoreButtons = screen.getAllByRole('button', { name: /restore/i });
    await user.click(restoreButtons[0]);

    expect(mockToast.addToast).toHaveBeenCalledWith('success', 'Session restored');
  });

  it('shows sfs pull guidance when restore response has local_copy_may_be_missing=true', async () => {
    // Configure mutate to invoke onSuccess with everywhere-delete restore response
    mockRestoreMutate.mockImplementation((_id: string, opts: { onSuccess?: (data: Record<string, unknown>) => void }) => {
      opts.onSuccess?.({ local_copy_may_be_missing: true, restored_from_scope: 'both' });
    });

    renderList();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /trash/i }));
    await waitFor(() => {
      expect(screen.getByText('Deleted session A')).toBeInTheDocument();
    });

    const restoreButtons = screen.getAllByRole('button', { name: /restore/i });
    await user.click(restoreButtons[0]);

    expect(mockToast.addToast).toHaveBeenCalledWith(
      'success',
      expect.stringContaining('sfs pull'),
    );
  });

  it('does not use "restored everywhere" wording', async () => {
    mockRestoreMutate.mockImplementation((_id: string, opts: { onSuccess?: (data: Record<string, unknown>) => void }) => {
      opts.onSuccess?.({ local_copy_may_be_missing: true, restored_from_scope: 'both' });
    });

    renderList();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /trash/i }));
    await waitFor(() => {
      expect(screen.getByText('Deleted session A')).toBeInTheDocument();
    });

    const restoreButtons = screen.getAllByRole('button', { name: /restore/i });
    await user.click(restoreButtons[0]);

    const toastMessage = mockToast.addToast.mock.calls[0]?.[1] ?? '';
    expect(toastMessage).not.toContain('restored everywhere');
    expect(toastMessage).not.toContain('fully restored');
  });
});

// ---- Test for bulk delete scope ----

describe('Bulk delete scope dialog', () => {
  let SessionList: typeof import('./SessionList').default;

  beforeEach(async () => {
    vi.resetModules();
    for (const h of Object.values(hooks)) h.mockReset();
    mockAuth.mockReset();
    mockToast.addToast.mockReset();

    const sessions = [
      baseSession({ id: 'ses_1', title: 'Session 1' }),
      baseSession({ id: 'ses_2', title: 'Session 2' }),
    ];

    hooks.useSessions.mockReturnValue({
      data: { sessions, total: 2, page: 1, page_size: 20, has_more: false },
      isLoading: false,
      error: null,
    });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });
    hooks.useDeletedSessions.mockReturnValue({ data: null, isLoading: false, error: null });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });

    const mockDeleteSession = vi.fn().mockResolvedValue({});
    mockAuth.mockReturnValue({
      auth: {
        apiKey: 'sk_test',
        baseUrl: 'http://test.api',
        client: {
          deleteSession: mockDeleteSession,
          listInbox: vi.fn().mockResolvedValue({ handoffs: [], total: 0 }),
        },
      },
    });

    const mod = await import('./SessionList');
    SessionList = mod.default;
  });

  function renderList() {
    return render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <SessionList />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('opens scope dialog on bulk delete and shows correct count', async () => {
    renderList();
    const user = userEvent.setup();

    // Enter select mode
    await user.click(screen.getByRole('button', { name: /select/i }));

    // Select sessions via checkboxes
    const checkboxes = screen.getAllByRole('checkbox');
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);

    // Click "Delete selected"
    await user.click(screen.getByRole('button', { name: /delete selected/i }));

    // The scope dialog should appear
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(/delete 2 sessions/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/remove from cloud/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/delete everywhere/i)).toBeInTheDocument();
  });
});
