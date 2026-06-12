import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SessionList from './SessionList';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useSessions: vi.fn(),
    useDeletedSessions: vi.fn(),
    useRestoreSession: vi.fn(),
    useFolders: vi.fn(),
    useAddBookmark: vi.fn(),
    useFolderSessions: vi.fn(),
  },
}));

vi.mock('../hooks/useSessions', () => ({
  useSessions: (...args: unknown[]) => hooks.useSessions(...args),
  useDeletedSessions: () => hooks.useDeletedSessions(),
  useRestoreSession: () => hooks.useRestoreSession(),
}));

vi.mock('../hooks/useBookmarks', () => ({
  useFolders: () => hooks.useFolders(),
  useAddBookmark: () => hooks.useAddBookmark(),
  useFolderSessions: (...args: unknown[]) => hooks.useFolderSessions(...args),
  useCreateFolder: () => ({ mutateAsync: vi.fn() }),
  useUpdateFolder: () => ({ mutateAsync: vi.fn() }),
  useDeleteFolder: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    auth: {
      client: {
        listSessions: vi.fn(),
        listHandoffs: vi.fn().mockResolvedValue({ handoffs: [] }),
        setAlias: vi.fn(),
        bulkDelete: vi.fn(),
      },
    },
  }),
}));

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: vi.fn(), removeToast: vi.fn(), toasts: [] }),
}));

vi.mock('@tanstack/react-query', async () => {
  const actual = await vi.importActual<typeof import('@tanstack/react-query')>('@tanstack/react-query');
  return {
    ...actual,
    useQuery: vi.fn().mockReturnValue({ data: null, isLoading: false }),
    useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  };
});

describe('SessionList empty state', () => {
  beforeEach(() => {
    hooks.useSessions.mockReset();
    hooks.useDeletedSessions.mockReset();
    hooks.useRestoreSession.mockReset();
    hooks.useFolders.mockReset();
    hooks.useAddBookmark.mockReset();
    hooks.useFolderSessions.mockReset();
  });

  it('shows "Get Started" link to /getting-started when no sessions', () => {
    hooks.useSessions.mockReturnValue({
      data: { sessions: [], total: 0, page: 1, page_size: 20, has_more: false },
      isLoading: false,
      error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: [], isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolders.mockReturnValue({ data: [], isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: [], isLoading: false });

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    expect(screen.getByText('No sessions yet')).toBeInTheDocument();
    expect(screen.getByText(/captures sessions automatically/i)).toBeInTheDocument();
    const getStartedLink = screen.getByRole('link', { name: /get started/i });
    expect(getStartedLink).toHaveAttribute('href', '/getting-started');
    const helpLink = screen.getByRole('link', { name: /view help/i });
    expect(helpLink).toHaveAttribute('href', '/help');
  });

  it('renders list view by default with session items', () => {
    const now = new Date().toISOString();
    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [
          {
            id: 'ses_1', title: 'Test Session', source_tool: 'claude-code',
            model_id: null, message_count: 5, total_input_tokens: 100, total_output_tokens: 200,
            created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
          },
          {
            id: 'ses_2', title: 'Another Session', source_tool: 'codex',
            model_id: 'claude-sonnet-4-6', message_count: 3, total_input_tokens: 50, total_output_tokens: 60,
            created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
          },
        ],
        total: 2, page: 1, page_size: 20, has_more: false,
      },
      isLoading: false, error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    // Session titles visible (hero + list — getAllByText handles multiples)
    expect(screen.getAllByText('Test Session').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Another Session')).toBeInTheDocument();
    // List view is the active toggle state
    expect(screen.getByLabelText('List view')).toHaveAttribute('aria-pressed', 'true');
  });

  it('switches to grid view and renders session cards', async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();
    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [
          {
            id: 'ses_1', title: 'Test Session', source_tool: 'claude-code',
            model_id: null, message_count: 5, total_input_tokens: 100, total_output_tokens: 200,
            created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
          },
        ],
        total: 1, page: 1, page_size: 20, has_more: false,
      },
      isLoading: false, error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    // Switch to grid view
    await user.click(screen.getByLabelText('Grid view'));
    expect(screen.getByLabelText('Grid view')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('List view')).toHaveAttribute('aria-pressed', 'false');
    // Session card still renders title (hero + grid card)
    expect(screen.getAllByText('Test Session').length).toBeGreaterThanOrEqual(1);
  });

  it('persists view mode across re-mounts via localStorage', () => {
    // Pre-set grid in localStorage
    window.localStorage.setItem('sfs-sessions-view', 'grid');

    const now = new Date().toISOString();
    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [
          {
            id: 'ses_1', title: 'X', source_tool: 'claude-code',
            model_id: null, message_count: 1, total_input_tokens: 0, total_output_tokens: 0,
            created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
          },
        ],
        total: 1, page: 1, page_size: 20, has_more: false,
      },
      isLoading: false, error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    // Should start in grid mode because localStorage says so
    expect(screen.getByLabelText('Grid view')).toHaveAttribute('aria-pressed', 'true');

    // Clean up
    window.localStorage.removeItem('sfs-sessions-view');
  });

  it('folder dropdown drives filter state when a folder is selected', async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();

    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [
          {
            id: 'ses_1', title: 'Test Session', source_tool: 'claude-code',
            model_id: null, message_count: 5, total_input_tokens: 100, total_output_tokens: 200,
            created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
          },
        ],
        total: 1, page: 1, page_size: 20, has_more: false,
      },
      isLoading: false, error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({
      data: {
        folders: [
          { id: 'fld_1', name: 'Bug fixes', color: '#f04060', bookmark_count: 3 },
          { id: 'fld_2', name: 'Onboarding', color: '#3ddc84', bookmark_count: 1 },
        ],
      },
      isLoading: false,
    });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    const folderSessions = vi.fn().mockReturnValue({ data: null, isLoading: false });
    hooks.useFolderSessions.mockImplementation(folderSessions);

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    // Open the folder dropdown
    await user.click(screen.getByText('All Sessions'));
    // Click a folder
    await user.click(screen.getByText('Bug fixes'));

    // useFolderSessions should have been called with the folder ID
    expect(folderSessions).toHaveBeenCalledWith('fld_1');
  });

  it('groups sessions by tool when "Sort: Tool" is selected', async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();

    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [
          {
            id: 'ses_codex1',
            title: 'Codex Session',
            source_tool: 'codex',
            model_id: null,
            message_count: 5,
            total_input_tokens: 0,
            total_output_tokens: 0,
            created_at: now,
            updated_at: now,
            last_user_at: now,
            tool_use_count: 0,
          },
          {
            id: 'ses_gemini1',
            title: 'Gemini Session',
            source_tool: 'gemini-cli',
            model_id: null,
            message_count: 3,
            total_input_tokens: 0,
            total_output_tokens: 0,
            created_at: now,
            updated_at: now,
            last_user_at: now,
            tool_use_count: 0,
          },
          {
            id: 'ses_claude1',
            title: 'Claude Session',
            source_tool: 'claude-code',
            model_id: null,
            message_count: 7,
            total_input_tokens: 0,
            total_output_tokens: 0,
            created_at: now,
            updated_at: now,
            last_user_at: now,
            tool_use_count: 0,
          },
        ],
        total: 3,
        page: 1,
        page_size: 20,
        has_more: false,
      },
      isLoading: false,
      error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });

    render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    await user.selectOptions(screen.getByDisplayValue('Sort: Date'), 'tool');

    expect(screen.queryByRole('heading', { name: 'Today' })).not.toBeInTheDocument();
    const groupHeadings = screen.getAllByRole('heading', { level: 3 }).map((el) => el.textContent);
    expect(groupHeadings).toEqual(['Claude Code', 'Codex', 'Gemini CLI']);
  });
});
