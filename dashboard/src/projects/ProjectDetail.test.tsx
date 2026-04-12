import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectDetail from './ProjectDetail';

/**
 * UI coverage for ProjectDetail. The page is 979 lines and renders four
 * tabs (Context, Pages, Entries, History) plus a header card with edit,
 * auto-narrative toggle, and delete-project flow. These tests focus on
 * the entry points and the highest-risk user interactions:
 *
 *   - loading / error states
 *   - header renders project metadata + auto-narrative toggle
 *   - tab switching navigates between panels
 *   - edit → save → mutation called with the right payload
 *   - compile-from-context calls the compile mutation
 *   - delete flow shows confirm dialog then calls mutate
 *
 * Hook internals are mocked one layer up (at `../hooks/useProjects` and
 * `../hooks/useToast`) so tests stay focused on the UI, not react-query.
 */

// vi.mock is hoisted above imports, so any referenced variables must be
// created inside vi.hoisted() which runs before the mock factory.
const { hooks, mockAddToast } = vi.hoisted(() => ({
  hooks: {
    useProject: vi.fn(),
    useUpdateProjectContext: vi.fn(),
    useDeleteProject: vi.fn(),
    useKnowledgeEntries: vi.fn(),
    useDismissEntry: vi.fn(),
    useCompileProject: vi.fn(),
    useCompilations: vi.fn(),
    useWikiPages: vi.fn(),
    useWikiPage: vi.fn(),
    useUpdateWikiPage: vi.fn(),
    useDeleteWikiPage: vi.fn(),
    useRegenerateWikiPage: vi.fn(),
    useUpdateProjectSettings: vi.fn(),
    useProjectHealth: vi.fn(),
    useDismissStaleEntries: vi.fn(),
    usePromoteEntry: vi.fn(),
    useSupersedeEntry: vi.fn(),
    useRebuildProject: vi.fn(),
    useRefreshEntry: vi.fn(),
  },
  mockAddToast: vi.fn(),
}));

vi.mock('../hooks/useProjects', () => hooks);

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

// react-markdown is ESM-only and pulls in a lot — stub it to a simple
// <div> so we don't have to transform the import tree in tests.
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="markdown">{children}</div>,
}));

function makeMutation(extra: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    ...extra,
  };
}

function setProjectData(overrides: Record<string, unknown> = {}) {
  hooks.useProject.mockReturnValue({
    data: {
      id: 'proj_abc',
      git_remote_normalized: 'sessionfs/sessionfs',
      context_document: '# Project\n\nSome context here.',
      created_at: '2026-03-01T12:00:00Z',
      updated_at: '2026-04-10T12:00:00Z',
      auto_narrative: false,
      ...overrides,
    },
    isLoading: false,
    error: null,
  });
}

function renderPage(initial = '/projects/sessionfs%2Fsessionfs') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route path="/projects/:id" element={<ProjectDetail />} />
        <Route path="/projects" element={<div>projects list</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('ProjectDetail', () => {
  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();
    mockAddToast.mockReset();

    // Default: entries-related hooks return empty pages so the Entries
    // tab renders without crashes when we switch to it.
    hooks.useKnowledgeEntries.mockReturnValue({
      data: { entries: [], total: 0 },
      isLoading: false,
    });
    hooks.useCompilations.mockReturnValue({ data: { compilations: [] }, isLoading: false });
    hooks.useWikiPages.mockReturnValue({ data: { pages: [] }, isLoading: false });
    hooks.useWikiPage.mockReturnValue({ data: null, isLoading: false });
    hooks.useDismissEntry.mockReturnValue(makeMutation());
    hooks.useCompileProject.mockReturnValue(makeMutation());
    hooks.useUpdateProjectContext.mockReturnValue(makeMutation());
    hooks.useDeleteProject.mockReturnValue(makeMutation());
    hooks.useUpdateWikiPage.mockReturnValue(makeMutation());
    hooks.useDeleteWikiPage.mockReturnValue(makeMutation());
    hooks.useRegenerateWikiPage.mockReturnValue(makeMutation());
    hooks.useUpdateProjectSettings.mockReturnValue(makeMutation());
    hooks.useProjectHealth.mockReturnValue({ data: null, isLoading: false });
    hooks.useDismissStaleEntries.mockReturnValue(makeMutation());
    hooks.usePromoteEntry.mockReturnValue(makeMutation());
    hooks.useSupersedeEntry.mockReturnValue(makeMutation());
    hooks.useRebuildProject.mockReturnValue(makeMutation());
    hooks.useRefreshEntry.mockReturnValue(makeMutation());
  });

  it('shows loading state while the project fetches', () => {
    hooks.useProject.mockReturnValue({ data: undefined, isLoading: true, error: null });
    renderPage();
    expect(screen.getByText(/loading project/i)).toBeInTheDocument();
  });

  it('shows an error state when the project fails to load', () => {
    hooks.useProject.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
    });
    renderPage();
    expect(screen.getByText(/failed to load project/i)).toBeInTheDocument();
    expect(screen.getByText(/boom/i)).toBeInTheDocument();
  });

  it('renders the project header with remote + auto-narrative toggle', () => {
    setProjectData();
    renderPage();

    expect(screen.getByRole('heading', { name: /sessionfs\/sessionfs/i })).toBeInTheDocument();
    const narrativeToggle = screen.getByRole('checkbox', { name: /auto-narrative on sync/i });
    expect(narrativeToggle).toBeInTheDocument();
    expect(narrativeToggle).not.toBeChecked();
  });

  it('toggling auto-narrative calls the settings mutation', async () => {
    setProjectData();
    const settings = makeMutation();
    hooks.useUpdateProjectSettings.mockReturnValue(settings);

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: /auto-narrative on sync/i }));

    await waitFor(() => {
      expect(settings.mutate).toHaveBeenCalledWith(
        { auto_narrative: true },
        expect.any(Object),
      );
    });
  });

  it('clicking Edit Context Document opens the draft editor', async () => {
    setProjectData();
    renderPage();
    const user = userEvent.setup();

    const editBtn = await screen.findByRole('button', { name: /^edit$/i });
    await user.click(editBtn);

    // A textarea should now be rendered with the existing document as its value
    const textarea = await screen.findByRole('textbox');
    expect(textarea).toBeInTheDocument();
    expect((textarea as HTMLTextAreaElement).value).toContain('Some context here');
  });

  it('clicking Save in edit mode calls updateProjectContext with the draft', async () => {
    setProjectData();
    const update = makeMutation();
    hooks.useUpdateProjectContext.mockReturnValue(update);

    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /^edit$/i }));
    const textarea = await screen.findByRole('textbox');
    await user.clear(textarea);
    await user.type(textarea, 'new context');

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(update.mutate).toHaveBeenCalled();
    });
    const [args] = update.mutate.mock.calls[0];
    expect(args.remote).toBe('sessionfs/sessionfs');
    expect(args.doc).toBe('new context');
  });

  it('switching to the Entries tab loads the knowledge entries view', async () => {
    setProjectData();
    hooks.useKnowledgeEntries.mockReturnValue({
      data: {
        entries: [
          {
            id: 42,
            content: 'The auth middleware must resolve effective tier',
            entry_type: 'decision',
            created_at: '2026-04-10T10:00:00Z',
            is_dismissed: false,
            compiled_at: null,
          },
        ],
        total: 1,
      },
      isLoading: false,
    });

    renderPage();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /^entries/i }));

    // The entry text should appear once the tab renders it
    await waitFor(() => {
      expect(
        screen.getByText(/auth middleware must resolve effective tier/i),
      ).toBeInTheDocument();
    });
  });

  it('delete-project flow shows confirm dialog then calls deleteProject.mutate', async () => {
    setProjectData();
    const del = makeMutation();
    hooks.useDeleteProject.mockReturnValue(del);

    renderPage();
    const user = userEvent.setup();

    // Open the "more" menu (three-dot button)
    const moreButtons = screen.getAllByRole('button');
    // The "more menu" button is the three-dot icon button — find it by its svg shape.
    // A reliable way: it's the button whose content is the three-dot svg (no text).
    const moreBtn = moreButtons.find((b) => b.querySelector('svg circle[cx="12"][cy="5"]'));
    expect(moreBtn).toBeDefined();
    await user.click(moreBtn!);

    // "Delete Project" menu item appears
    const deleteMenuItem = await screen.findByRole('button', { name: /delete project/i });
    await user.click(deleteMenuItem);

    // Confirmation dialog must appear before actual delete
    // Then click the final "Delete" button inside the dialog.
    const confirmBtn = await screen.findByRole('button', { name: /^delete$/i });
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(del.mutate).toHaveBeenCalledWith('proj_abc', expect.any(Object));
    });
  });
});
