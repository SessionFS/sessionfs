import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import type { NavigateFunction } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SessionList from './SessionList';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useSessions: vi.fn(),
    useSessionModels: vi.fn(),
    useDeletedSessions: vi.fn(),
    useRestoreSession: vi.fn(),
    useFolders: vi.fn(),
    useAddBookmark: vi.fn(),
    useFolderSessions: vi.fn(),
  },
}));

vi.mock('../hooks/useSessions', () => ({
  useSessions: (...args: unknown[]) => hooks.useSessions(...args),
  useSessionModels: (...args: unknown[]) => hooks.useSessionModels(...args),
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
    hooks.useSessionModels.mockReset();
    hooks.useSessionModels.mockReturnValue({ data: { models: [] }, isLoading: false });
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
    // The page carries a single h1 and the controls are real buttons
    expect(screen.getByRole('heading', { level: 1, name: 'Sessions' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Select' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Sort direction/ })).toBeInTheDocument();
  });

  it('shows labelled checkboxes in select mode', async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();
    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [{
          id: 'ses_1', title: 'Test Session', source_tool: 'claude-code',
          model_id: null, message_count: 5, total_input_tokens: 100, total_output_tokens: 200,
          created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
        }],
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

    await user.click(screen.getByRole('button', { name: 'Select' }));
    const box = screen.getByRole('checkbox', { name: 'Select Test Session' });
    await user.click(box);
    expect(box).toBeChecked();
    expect(screen.getByText('1 selected')).toBeInTheDocument();
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

  it('keeps session rows free of nested interactive elements in list and grid views', async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();
    hooks.useSessions.mockReturnValue({
      data: {
        sessions: [{
          id: 'ses_1', title: 'Nested Check', source_tool: 'claude-code',
          model_id: null, message_count: 5, total_input_tokens: 100, total_output_tokens: 200,
          created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
        }],
        total: 1, page: 1, page_size: 20, has_more: false,
      },
      isLoading: false, error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });

    const { container } = render(
      <MemoryRouter>
        <SessionList />
      </MemoryRouter>,
    );

    const nested = () => container.querySelectorAll('[role="button"] button, [role="button"] input, button button, button input');
    expect(screen.getAllByRole('button', { name: /Nested Check/ }).length).toBeGreaterThan(0);
    expect(nested()).toHaveLength(0);

    await user.click(screen.getByLabelText('Grid view'));
    expect(screen.getAllByRole('button', { name: /Nested Check/ }).length).toBeGreaterThan(0);
    expect(nested()).toHaveLength(0);

    // In select mode (list view) the title button toggles selection instead of navigating
    await user.click(screen.getByLabelText('List view'));
    await user.click(screen.getByRole('button', { name: 'Select' }));
    const box = screen.getByRole('checkbox', { name: 'Select Nested Check' });
    expect(box).not.toBeChecked();
    const titleBtn = screen.getAllByRole('button', { name: /Nested Check/ }).find((b) => b.closest('.group'));
    await user.click(titleBtn!);
    expect(box).toBeChecked();
    expect(nested()).toHaveLength(0);
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

    // Open the sort combobox and select "Sort: Tool"
    await user.click(screen.getByRole('button', { name: 'Sort sessions' }));
    await user.click(
      screen.getByRole('option', { name: 'Sort: Tool' }),
    );

    expect(screen.queryByRole('heading', { name: 'Today' })).not.toBeInTheDocument();
    const groupHeadings = screen.getAllByRole('heading', { level: 2 }).map((el) => el.textContent);
    expect(groupHeadings).toEqual(['Claude Code', 'Codex', 'Gemini CLI']);
  });

  it('populates the model filter from the models endpoint and syncs selection to URL + query', async () => {
    const user = userEvent.setup();

    hooks.useSessions.mockReturnValue({
      data: { sessions: [], total: 0, page: 1, page_size: 20, has_more: false },
      isLoading: false, error: null,
    });
    hooks.useSessionModels.mockReturnValue({
      data: {
        models: [
          { model_id: 'gpt-5-20250127', count: 3 },
          { model_id: null, count: 1 },
        ],
      },
      isLoading: false,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });

    let lastSearch = '';
    function Probe() {
      lastSearch = useLocation().search;
      return null;
    }

    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <Probe />
        <SessionList />
      </MemoryRouter>,
    );

    // The trigger shows the "All Models" default
    expect(screen.getByRole('button', { name: 'Filter by model' })).toHaveTextContent('All Models');

    // Open and select the dated model, then the unknown bucket
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    await user.click(screen.getByRole('checkbox', { name: /gpt-5/ }));
    await user.click(screen.getByRole('checkbox', { name: /Unknown model/ }));

    // List query carries the raw id plus the unknown sentinel
    expect(hooks.useSessions).toHaveBeenLastCalledWith(expect.objectContaining({
      model: ['gpt-5-20250127', 'unknown'],
    }));
    // URL reflects the selection as repeatable ?model=
    expect(lastSearch).toBe('?model=gpt-5-20250127&model=unknown');
  });
});


describe('SessionList model filter + model sort', () => {
  const now = new Date().toISOString();
  const row = (id: string, model_id: string | null) => ({
    id, title: `Session ${id}`, source_tool: 'claude-code', model_id,
    message_count: 1, total_input_tokens: 0, total_output_tokens: 0,
    created_at: now, updated_at: now, last_user_at: now, tool_use_count: 0,
  });

  let lastSearch = '';
  let nav: NavigateFunction = () => {};
  function Probe() {
    lastSearch = useLocation().search;
    nav = useNavigate();
    return null;
  }

  function setup({
    sessions = [row('ses_1', 'gpt-5-20250127')],
    hasMore = false,
    models = [{ model_id: 'gpt-5-20250127', count: 3 }, { model_id: null, count: 1 }],
    modelsLoading = false,
  }: {
    sessions?: ReturnType<typeof row>[];
    hasMore?: boolean;
    models?: { model_id: string | null; count: number }[];
    modelsLoading?: boolean;
  } = {}) {
    hooks.useSessions.mockReset();
    hooks.useSessions.mockReturnValue({
      data: { sessions, total: sessions.length, page: 1, page_size: 20, has_more: hasMore },
      isLoading: false, error: null,
    });
    hooks.useSessionModels.mockReset();
    hooks.useSessionModels.mockReturnValue({
      data: modelsLoading ? undefined : { models },
      isLoading: modelsLoading,
      error: null,
    });
    hooks.useDeletedSessions.mockReturnValue({ data: { sessions: [] }, isLoading: false });
    hooks.useRestoreSession.mockReturnValue({ mutate: vi.fn(), isPending: false });
    hooks.useFolders.mockReturnValue({ data: { folders: [] }, isLoading: false });
    hooks.useAddBookmark.mockReturnValue({ mutateAsync: vi.fn() });
    hooks.useFolderSessions.mockReturnValue({ data: null, isLoading: false });
  }

  function renderAt(url: string) {
    return render(
      <MemoryRouter initialEntries={[url]}>
        <Probe />
        <SessionList />
      </MemoryRouter>,
    );
  }

  /** The main list query is the LAST useSessions call of each render. */
  const lastListParams = () => hooks.useSessions.mock.calls.at(-1)![0];

  it('derives the filter from the URL, including a shared link with a model the list does not know', async () => {
    const user = userEvent.setup();
    setup();
    renderAt('/sessions?model=shared-gone-model');

    expect(lastListParams()).toEqual(expect.objectContaining({ model: ['shared-gone-model'] }));
    const trigger = screen.getByRole('button', { name: 'Filter by model' });
    expect(trigger).toHaveTextContent('shared-gone-model');

    await user.click(trigger);
    const stale = screen.getByRole('checkbox', { name: /shared-gone-model/ });
    expect(stale).toBeChecked();
    await user.click(stale);

    expect(lastSearch).toBe('');
    expect(lastListParams().model).toBeUndefined();
  });

  it('follows back/forward and sidebar navigation without a stale mirror', () => {
    setup();
    renderAt('/sessions?model=gpt-5-20250127');
    expect(lastListParams().model).toEqual(['gpt-5-20250127']);

    act(() => nav('/sessions?model=unknown'));
    expect(lastListParams().model).toEqual(['unknown']);
    expect(screen.getByRole('button', { name: 'Filter by model' })).toHaveTextContent('Unknown model');

    act(() => nav(-1));
    expect(lastListParams().model).toEqual(['gpt-5-20250127']);

    // Sidebar "Sessions" link: no query string at all
    act(() => nav('/sessions'));
    expect(lastListParams().model).toBeUndefined();
    expect(screen.getByRole('button', { name: 'Filter by model' })).toHaveTextContent('All Models');
  });

  it('resets to page 1 when the model selection changes', async () => {
    const user = userEvent.setup();
    setup({ hasMore: true });
    renderAt('/sessions');

    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(lastListParams().page).toBe(2);

    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    await user.click(screen.getByRole('checkbox', { name: /gpt-5/ }));
    expect(lastListParams()).toEqual(expect.objectContaining({ page: 1, model: ['gpt-5-20250127'] }));
  });

  it('keeps model selections when the Tool filter changes (shown at 0 if absent)', async () => {
    const user = userEvent.setup();
    setup();
    renderAt('/sessions?model=gpt-5-20250127');

    // The Tool filter narrows /models to a list without the selected model
    hooks.useSessionModels.mockReturnValue({
      data: { models: [{ model_id: 'codex-4-1', count: 2 }] }, isLoading: false, error: null,
    });
    await user.click(screen.getByRole('button', { name: 'Filter by tool' }));
    await user.click(screen.getByRole('option', { name: 'Codex' }));

    expect(lastListParams()).toEqual(expect.objectContaining({
      source_tool: 'codex', model: ['gpt-5-20250127'],
    }));
    expect(hooks.useSessionModels).toHaveBeenLastCalledWith({ source_tool: 'codex' });
    expect(lastSearch).toBe('?model=gpt-5-20250127');

    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    const kept = screen.getByRole('checkbox', { name: /gpt-5/ });
    expect(kept).toBeChecked();
    expect(kept.closest('div')).toHaveTextContent('0');
  });

  it('sends model sort to the server (spanning pages) and leaves other sorts client-side', async () => {
    const user = userEvent.setup();
    setup({
      // Server order under sort=model must be kept as-is, not re-sorted
      sessions: [row('ses_b', 'zeta'), row('ses_a', 'alpha'), row('ses_n', null)],
    });
    renderAt('/sessions');
    expect(lastListParams().sort).toBeUndefined();
    expect(lastListParams().order).toBeUndefined();

    await user.click(screen.getByRole('button', { name: 'Sort sessions' }));
    await user.click(screen.getByRole('option', { name: 'Sort: Model' }));
    expect(lastListParams()).toEqual(expect.objectContaining({ sort: 'model', order: 'desc', page: 1 }));
    const titles = screen.getAllByRole('button', { name: /^Session ses_/ }).map((b) => b.textContent);
    expect(titles).toEqual(['Session ses_b', 'Session ses_a', 'Session ses_n']);

    await user.click(screen.getByRole('button', { name: /Sort direction/ }));
    expect(lastListParams()).toEqual(expect.objectContaining({ sort: 'model', order: 'asc' }));

    await user.click(screen.getByRole('button', { name: 'Sort sessions' }));
    await user.click(screen.getByRole('option', { name: 'Sort: Title' }));
    expect(lastListParams().sort).toBeUndefined();
    expect(lastListParams().order).toBeUndefined();
  });

  it('does not flash "No models yet" while the models list is loading', async () => {
    const user = userEvent.setup();
    setup({ modelsLoading: true });
    renderAt('/sessions');
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    expect(screen.getByText(/Loading models/)).toBeInTheDocument();
    expect(screen.queryByText('No models yet')).not.toBeInTheDocument();
  });
});
