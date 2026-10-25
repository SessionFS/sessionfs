import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectDetail from './ProjectDetail';
import { ApiError } from '../api/client';

/**
 * UI coverage for ProjectDetail. The page has five URL-driven sections
 * (Overview, Knowledge, Work, Rules & Skills, Settings) with sub-tabs, plus
 * a header card. These tests focus on the entry points and the highest-risk
 * user interactions:
 *
 *   - loading / error states
 *   - header renders project metadata
 *   - sections, sub-tabs and the ?tab=/&sub= URL round trip
 *   - Settings holds rename, auto-narrative, repos, merge, transfer, delete
 *   - edit → save → mutation called with the right payload
 *   - compile-from-context calls the compile mutation
 *   - delete flow shows confirm dialog then calls mutate
 *
 * Hook internals are mocked one layer up (at `../hooks/useProjects` and
 * `../hooks/useToast`) so tests stay focused on the UI, not react-query.
 */

// vi.mock is hoisted above imports, so any referenced variables must be
// created inside vi.hoisted() which runs before the mock factory.
const { hooks, mockAddToast, meData, jiraSurface } = vi.hoisted(() => ({
  hooks: {
    useProject: vi.fn(),
    useProjects: vi.fn(),
    useUpdateProjectContext: vi.fn(),
    useDeleteProject: vi.fn(),
    useKnowledgeEntries: vi.fn(),
    useDismissEntry: vi.fn(),
    useCompileProject: vi.fn(),
    useCompileStatus: vi.fn(),
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
    useUpdateProjectName: vi.fn(),
    useProjectRepos: vi.fn(),
    useLinkRepo: vi.fn(),
    useUnlinkRepo: vi.fn(),
    useMergeProject: vi.fn(),
  },
  mockAddToast: vi.fn(),
  meData: { value: { user_id: 'u_1', tier: 'enterprise', effective_tier: 'enterprise' } },
  jiraSurface: { value: null as { isEnterprise?: boolean } | null },
}));

vi.mock('../hooks/useProjects', () => hooks);

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

vi.mock('../helper/HelperProvider', () => ({
  useHelper: () => ({ openHelper: vi.fn() }),
}));

// v0.10.0 Phase 4 added a Transfer tab to ProjectDetail. The
// `useMyOrgs` hook calls `useAuth()` which would require an
// AuthProvider; the inbox + panel surfaces are exercised by their own
// component tests, so we stub the hook here to avoid wiring the auth
// context for the broader ProjectDetail suite.
vi.mock('../transfers/useTransfers', () => ({
  useMyOrgs: () => ({ data: [], isLoading: false, error: null }),
}));

// The Jira ticket surface resolves the current user's tier/role; stub the me
// query the same way so the broader suite stays free of an AuthProvider. The
// holder lets individual tests vary the caller's tier.
vi.mock('../hooks/useMe', () => ({
  useMe: () => ({ data: meData.value, isLoading: false }),
}));

// The Work and Rules & Skills bodies have their own suites and their own
// auth-bound hooks; here only which one is mounted matters. Capture the Jira
// surface prop so we can assert the enterprise gate resolves from the project.
vi.mock('./TicketsTab', () => ({
  default: (props: { projectId: string; jira?: { isEnterprise?: boolean } }) => {
    jiraSurface.value = props.jira ?? null;
    return <div>tickets body</div>;
  },
}));
vi.mock('./AgentRunsTab', () => ({ default: () => <div>agent runs body</div> }));
vi.mock('./PersonasTab', () => ({ default: () => <div>personas body</div> }));
vi.mock('./RulesTab', () => ({ default: () => <div>rules body</div> }));
vi.mock('./MarketplaceTab', () => ({ default: () => <div>skills body</div> }));
// TransferPanel has its own suite; only its heading matters here.
vi.mock('./TransferPanel', () => ({ default: () => <h3>Transfer ownership</h3> }));

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

function makeCompileStatus(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      last_compile_at: null,
      pending_claims: 0,
      compile_needed: false,
      compile_schedule: 'nightly',
      compile_after_claims: null,
      last_scheduled_compile_at: null,
      next_scheduled_at: null,
      compile_history: [],
      ...overrides,
    },
    isLoading: false,
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

function LocationProbe() {
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <div data-testid="loc">{loc.search}</div>
      <button onClick={() => navigate(-1)}>history back</button>
    </>
  );
}

function renderPage(initial = '/projects/sessionfs%2Fsessionfs') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LocationProbe />
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
    meData.value = { user_id: 'u_1', tier: 'enterprise', effective_tier: 'enterprise' };
    jiraSurface.value = null;

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
    hooks.useCompileStatus.mockReturnValue(makeCompileStatus());
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
    hooks.useUpdateProjectName.mockReturnValue(makeMutation());
    hooks.useProjectRepos.mockReturnValue({ data: [], isLoading: false, error: null });
    hooks.useLinkRepo.mockReturnValue(makeMutation());
    hooks.useUnlinkRepo.mockReturnValue(makeMutation());
    hooks.useMergeProject.mockReturnValue(makeMutation());
    hooks.useProjects.mockReturnValue({ data: [], isLoading: false, error: null });
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

  it('renders the project header with the remote and no settings controls', () => {
    setProjectData();
    renderPage();

    expect(screen.getByRole('heading', { name: /sessionfs\/sessionfs/i })).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /auto-narrative on sync/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /rename project/i })).not.toBeInTheDocument();
  });

  it('toggling auto-narrative in Settings calls the settings mutation', async () => {
    setProjectData();
    const settings = makeMutation();
    hooks.useUpdateProjectSettings.mockReturnValue(settings);

    renderPage('/projects/sessionfs%2Fsessionfs?tab=settings');
    const user = userEvent.setup();
    const toggle = screen.getByRole('checkbox', { name: /auto-narrative on sync/i });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);

    await waitFor(() => {
      expect(settings.mutate).toHaveBeenCalledWith(
        { auto_narrative: true },
        expect.any(Object),
      );
    });
  });

  it('renaming from Settings calls the rename mutation', async () => {
    setProjectData({ name: 'Old name' });
    const rename = makeMutation();
    hooks.useUpdateProjectName.mockReturnValue(rename);

    renderPage('/projects/sessionfs%2Fsessionfs?tab=settings');
    const user = userEvent.setup();
    const input = screen.getByRole('textbox', { name: 'Project name' });
    expect(screen.getByRole('button', { name: 'Rename' })).toBeDisabled();
    await user.clear(input);
    await user.type(input, 'New name');
    await user.click(screen.getByRole('button', { name: 'Rename' }));

    expect(rename.mutate).toHaveBeenCalledWith('New name', expect.any(Object));
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

  it('switching to the Knowledge section loads the knowledge entries view', async () => {
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

    await user.click(screen.getByRole('tab', { name: /knowledge/i }));

    // The entry text should appear once the tab renders it
    await waitFor(() => {
      expect(
        screen.getByText(/auth middleware must resolve effective tier/i),
      ).toBeInTheDocument();
    });
  });

  // GitHub #86/#87: all five sections live in the one tablist (no horizontal
  // scroll needed), and the project's skills sub-tab is not named like the
  // sidebar Marketplace.
  it('renders the five sections inside a single tablist', () => {
    setProjectData();
    renderPage();
    const list = screen.getByRole('tablist');
    const names = screen.getAllByRole('tab').map((t) => t.textContent?.trim());
    expect(names).toEqual(['Overview', 'Knowledge', 'Work', 'Rules & Skills', 'Settings']);
    for (const tab of screen.getAllByRole('tab')) expect(list.contains(tab)).toBe(true);
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tab', { name: 'Marketplace' })).not.toBeInTheDocument();
  });

  it('puts the pending count on the Knowledge tab and the Entries sub-tab', async () => {
    setProjectData();
    hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 7 }, isLoading: false });
    renderPage();
    expect(screen.getByRole('tab', { name: /knowledge/i })).toHaveTextContent('7');
    await userEvent.setup().click(screen.getByRole('tab', { name: /knowledge/i }));
    expect(screen.getByRole('tab', { name: /entries/i })).toHaveTextContent('7');
  });

  it('shows sub-tabs per section and none for Overview or Settings', async () => {
    setProjectData();
    renderPage();
    const user = userEvent.setup();
    expect(screen.getAllByRole('tab')).toHaveLength(5);

    await user.click(screen.getByRole('tab', { name: 'Knowledge' }));
    expect(screen.getAllByRole('tab').map((t) => t.textContent?.trim().replace(/\d+$/, ''))).toEqual([
      'Overview', 'Knowledge', 'Work', 'Rules & Skills', 'Settings', 'Entries', 'Pages', 'History',
    ]);

    await user.click(screen.getByRole('tab', { name: 'Work' }));
    expect(screen.getByRole('tab', { name: 'Tickets' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Agent runs' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Personas' })).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: 'Rules & Skills' }));
    expect(screen.getByRole('tab', { name: 'Rules' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Skills' })).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: 'Settings' }));
    expect(screen.getAllByRole('tab')).toHaveLength(5);
  });

  describe('URL-driven tabs', () => {
    it('reads ?tab= and &sub= on load', () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?tab=knowledge&sub=history');
      expect(screen.getByRole('tab', { name: 'Knowledge' })).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
    });

    it('writes the section and sub-tab to the URL on click, keeping other params', async () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?edit=1');
      const user = userEvent.setup();

      await user.click(screen.getByRole('tab', { name: 'Work' }));
      expect(screen.getByTestId('loc')).toHaveTextContent('tab=work');
      expect(screen.getByTestId('loc')).toHaveTextContent('edit=1');

      await user.click(screen.getByRole('tab', { name: 'Personas' }));
      expect(screen.getByTestId('loc')).toHaveTextContent('tab=work');
      expect(screen.getByTestId('loc')).toHaveTextContent('sub=personas');

      // Changing section drops the previous section's sub-tab.
      await user.click(screen.getByRole('tab', { name: 'Knowledge' }));
      expect(screen.getByTestId('loc')).not.toHaveTextContent('sub=personas');
    });

    it('Back returns to the previous section; sub-tab clicks do not add history', async () => {
      setProjectData();
      renderPage();
      const user = userEvent.setup();
      await user.click(screen.getByRole('tab', { name: 'Work' }));
      await user.click(screen.getByRole('tab', { name: 'Agent runs' }));
      await user.click(screen.getByRole('tab', { name: 'Personas' }));
      await user.click(screen.getByRole('button', { name: 'history back' }));
      expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    });

    it('falls back to Overview and the first sub-tab for unknown values', () => {
      setProjectData();
      const { unmount } = renderPage('/projects/sessionfs%2Fsessionfs?tab=nonsense');
      expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
      unmount();
      renderPage('/projects/sessionfs%2Fsessionfs?tab=work&sub=nonsense');
      expect(screen.getByRole('tab', { name: 'Tickets' })).toHaveAttribute('aria-selected', 'true');
    });

    it.each([
      ['entries', 'Knowledge', 'Entries'],
      ['pages', 'Knowledge', 'Pages'],
      ['history', 'Knowledge', 'History'],
      ['tickets', 'Work', 'Tickets'],
      ['agent-runs', 'Work', 'Agent runs'],
      ['personas', 'Work', 'Personas'],
      ['rules', 'Rules & Skills', 'Rules'],
      ['marketplace', 'Rules & Skills', 'Skills'],
      ['repos', 'Settings', null],
      ['merge', 'Settings', null],
      ['transfer', 'Settings', null],
      ['context', 'Overview', null],
    ])('maps the legacy ?tab=%s key to %s', (legacy, section, subTab) => {
      setProjectData();
      renderPage(`/projects/sessionfs%2Fsessionfs?tab=${legacy}`);
      expect(screen.getByRole('tab', { name: section })).toHaveAttribute('aria-selected', 'true');
      if (subTab) {
        expect(screen.getByRole('tab', { name: new RegExp(`^${subTab}\\d*$`) })).toHaveAttribute('aria-selected', 'true');
      }
    });

    it('keeps ?edit=1 opening the context editor', async () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?edit=1');
      const textarea = await screen.findByRole('textbox');
      expect((textarea as HTMLTextAreaElement).value).toContain('Some context here');
    });
  });

  describe('Settings section', () => {
    it('holds rename, auto-narrative, repos, merge, transfer and the danger zone', () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?tab=settings');
      for (const name of [/project name/i, /auto-narrative/i, /repositories/i, /merge into this project/i, /transfer ownership/i, /danger zone/i]) {
        expect(screen.getByRole('heading', { name })).toBeInTheDocument();
      }
      expect(screen.getByRole('button', { name: /delete project/i })).toBeInTheDocument();
    });
  });

  it('the header no longer offers a project actions menu', () => {
    setProjectData();
    renderPage();
    expect(screen.queryByRole('button', { name: /project actions/i })).not.toBeInTheDocument();
  });

  it('shows one Compile action, in the pending notice, not on the Overview card', () => {
    setProjectData();
    hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 3 }, isLoading: false });
    renderPage();
    expect(screen.getAllByRole('button', { name: /compile now/i })).toHaveLength(1);
    expect(screen.getByText(/3 pending entries/i)).toBeInTheDocument();
  });

  describe('Jira enterprise gate (tk_792504a140604e62)', () => {
    it('resolves the gate from the project tier, not the caller primary org', async () => {
      setProjectData({ org_id: 'org_x', effective_tier: 'enterprise' });
      meData.value = { user_id: 'u_1', tier: 'team', effective_tier: 'team' };
      renderPage('/projects/proj_abc?tab=work&sub=tickets');
      await screen.findByText('tickets body');
      expect(jiraSurface.value?.isEnterprise).toBe(true);
    });

    it('falls back to the caller tier when the project omits effective_tier', async () => {
      setProjectData({ org_id: 'org_x' }); // no effective_tier on the project
      meData.value = { user_id: 'u_1', tier: 'team', effective_tier: 'team' };
      renderPage('/projects/proj_abc?tab=work&sub=tickets');
      await screen.findByText('tickets body');
      expect(jiraSurface.value?.isEnterprise).toBe(false);
    });
  });

  describe('compile-needed banner (GitHub #93)', () => {
    it('shows the banner with a one-click compile when the context is stale', async () => {
      setProjectData();
      hooks.useCompileStatus.mockReturnValue(
        makeCompileStatus({ compile_needed: true, pending_claims: 7 }),
      );
      const compile = makeMutation();
      hooks.useCompileProject.mockReturnValue(compile);
      renderPage();

      expect(screen.getByText(/compile needed/i)).toBeInTheDocument();
      expect(screen.getByText(/7 pending claims not yet in context/i)).toBeInTheDocument();
      await userEvent.setup().click(screen.getByRole('button', { name: /compile now/i }));
      expect(compile.mutate).toHaveBeenCalled();
    });

    it('hides the banner when the context is fresh', () => {
      setProjectData();
      hooks.useCompileStatus.mockReturnValue(
        makeCompileStatus({ compile_needed: false, pending_claims: 2, last_compile_at: '2026-10-23T10:00:00Z' }),
      );
      renderPage();
      expect(screen.queryByText(/compile needed/i)).not.toBeInTheDocument();
    });

    it('shows the last-compiled and pending-claims line in the Overview card', () => {
      setProjectData();
      hooks.useCompileStatus.mockReturnValue(
        makeCompileStatus({ pending_claims: 5, last_compile_at: '2026-10-23T10:00:00Z' }),
      );
      renderPage();
      expect(screen.getByText(/5 pending claims/i)).toBeInTheDocument();
      expect(screen.getByText(/last compiled/i)).toBeInTheDocument();
    });

    it('shows "Never compiled" when there is no compilation yet', () => {
      setProjectData();
      hooks.useCompileStatus.mockReturnValue(
        makeCompileStatus({ pending_claims: 1, last_compile_at: null }),
      );
      renderPage();
      expect(screen.getByText(/never compiled/i)).toBeInTheDocument();
    });
  });

  it('delete-project flow shows confirm dialog then calls deleteProject.mutate', async () => {
    setProjectData();
    const del = makeMutation();
    hooks.useDeleteProject.mockReturnValue(del);

    renderPage('/projects/sessionfs%2Fsessionfs?tab=settings');
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /delete project/i }));

    // Confirmation dialog must appear before actual delete.
    const confirmBtn = await screen.findByRole('button', { name: /^delete$/i });
    expect(del.mutate).not.toHaveBeenCalled();
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(del.mutate).toHaveBeenCalledWith('proj_abc', expect.any(Object));
    });
  });
});

describe('ProjectDetail routing by id', () => {
  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();
    hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 0 }, isLoading: false });
    hooks.useCompileProject.mockReturnValue(makeMutation());
    hooks.useCompileStatus.mockReturnValue(makeCompileStatus());
    hooks.useUpdateProjectContext.mockReturnValue(makeMutation());
    hooks.useDeleteProject.mockReturnValue(makeMutation());
    hooks.useUpdateProjectSettings.mockReturnValue(makeMutation());
    hooks.useUpdateProjectName.mockReturnValue(makeMutation());
    hooks.useProjectHealth.mockReturnValue({ data: null, isLoading: false });
    hooks.useProjectRepos.mockReturnValue({ data: [], isLoading: false, error: null });
    hooks.useProjects.mockReturnValue({ data: [], isLoading: false, error: null });
    for (const k of ['useLinkRepo', 'useUnlinkRepo', 'useMergeProject'] as const) {
      hooks[k].mockReturnValue(makeMutation());
    }
  });

  it('redirects a legacy remote URL to the id URL, keeping the query', async () => {
    setProjectData();
    renderPage('/projects/sessionfs%2Fsessionfs?tab=work&sub=personas');
    await waitFor(() => {
      expect(hooks.useProject).toHaveBeenLastCalledWith('proj_abc');
    });
    expect(screen.getByTestId('loc').textContent).toBe('?tab=work&sub=personas');
    expect(hooks.useProject).toHaveBeenCalledWith('sessionfs/sessionfs');
  });

  it('does not redirect when the URL already holds the project id', async () => {
    setProjectData();
    renderPage('/projects/proj_abc?tab=settings');
    await screen.findByRole('heading', { name: /sessionfs\/sessionfs/i });
    expect(screen.getByTestId('loc').textContent).toBe('?tab=settings');
    for (const [arg] of hooks.useProject.mock.calls) expect(arg).toBe('proj_abc');
  });

  it('follows a 410 merged_into to the target project id', async () => {
    const merged = new ApiError(410, JSON.stringify({
      error: { code: '410', message: 'merged', details: { error: 'project_merged', merged_into: 'proj_target' } },
    }));
    hooks.useProject.mockImplementation((arg: string) =>
      arg === 'proj_old'
        ? { data: undefined, isLoading: false, error: merged }
        : {
            data: { id: 'proj_target', git_remote_normalized: 'acme/new', context_document: '', created_at: '', updated_at: '' },
            isLoading: false,
            error: null,
          },
    );
    renderPage('/projects/proj_old?tab=settings');
    await waitFor(() => {
      expect(hooks.useProject).toHaveBeenLastCalledWith('proj_target');
    });
    expect(screen.getByTestId('loc').textContent).toBe('?tab=settings');
  });

  it('saves the context against the loaded project remote, not the URL param', async () => {
    setProjectData();
    const update = makeMutation();
    hooks.useUpdateProjectContext.mockReturnValue(update);
    renderPage('/projects/proj_abc');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /^edit$/i }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(update.mutate).toHaveBeenCalled());
    expect(update.mutate.mock.calls[0][0].remote).toBe('sessionfs/sessionfs');
  });
});
