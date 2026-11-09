import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
const { hooks, mockAddToast, helperOpen, meData, jiraSurface, ticketViewer, myOrgsData, confluenceLinks } = vi.hoisted(() => ({
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
    useSetWikiPagePublish: vi.fn(),
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
  helperOpen: vi.fn(),
  meData: { value: { user_id: 'u_1', tier: 'enterprise', effective_tier: 'enterprise' } },
  jiraSurface: { value: null as { isEnterprise?: boolean } | null },
  ticketViewer: { value: null as { isProjectAdmin?: boolean } | null },
  myOrgsData: { value: [{ org_id: 'org_x', name: 'X', role: 'member' }] as { org_id: string; name: string; role: string }[] },
  confluenceLinks: {
    value: undefined as { links: unknown[]; mapping: { publish_context: boolean } } | undefined,
    enabled: [] as boolean[],
  },
}));

vi.mock('../hooks/useProjects', () => hooks);

// The context "Publish to Confluence" button follows the links endpoint's
// mapping.publish_context; capture whether the query was enabled.
vi.mock('../hooks/useConfluencePublish', () => ({
  useConfluenceLinks: (_projectId: string | undefined, enabled: boolean) => {
    confluenceLinks.enabled.push(enabled);
    return { data: enabled ? confluenceLinks.value : undefined };
  },
  useConfluencePreview: () => ({ isLoading: true }),
  usePublishConfluence: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useResolveConfluenceLink: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUnlinkConfluence: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

vi.mock('../helper/HelperProvider', () => ({
  useHelper: () => ({ openHelper: helperOpen }),
}));

// v0.10.0 Phase 4 added a Transfer tab to ProjectDetail. The
// `useMyOrgs` hook calls `useAuth()` which would require an
// AuthProvider; the inbox + panel surfaces are exercised by their own
// component tests, so we stub the hook here to avoid wiring the auth
// context for the broader ProjectDetail suite.
vi.mock('../transfers/useTransfers', () => ({
  useMyOrgs: () => ({ data: myOrgsData.value, isLoading: false, error: null }),
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
  default: (props: {
    projectId: string;
    jira?: { isEnterprise?: boolean };
    params?: { view: string; group: string; q: string };
    onParamsChange?: (next: Record<string, unknown>) => void;
    viewer?: { isProjectAdmin?: boolean };
  }) => {
    jiraSurface.value = props.jira ?? null;
    ticketViewer.value = props.viewer ?? null;
    return (
      <div>
        tickets body
        <button onClick={() => props.onParamsChange?.({ ...props.params, view: props.params?.view === 'mine' ? 'all' : 'mine' })}>
          view: {props.params?.view}
        </button>
        <button onClick={() => props.onParamsChange?.({ ...props.params, group: 'issue', q: 'webhook' })}>
          group: {props.params?.group}
        </button>
      </div>
    );
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

// The section is a menu in the one-line project bar, not a row of tabs.
const sectionButton = () => screen.getByRole('button', { name: /^Section: / });
const escapeRe = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
async function goToSection(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(sectionButton());
  await user.click(screen.getByRole('menuitem', { name: new RegExp(`^${escapeRe(name)}`) }));
}
function expectSection(name: string) {
  expect(sectionButton()).toHaveAccessibleName(`Section: ${name}`);
}

describe('ProjectDetail', () => {
  beforeEach(() => {
    for (const h of Object.values(hooks)) h.mockReset();
    mockAddToast.mockReset();
    meData.value = { user_id: 'u_1', tier: 'enterprise', effective_tier: 'enterprise' };
    jiraSurface.value = null;
    myOrgsData.value = [{ org_id: 'org_x', name: 'X', role: 'member' }];
    confluenceLinks.value = undefined;
    confluenceLinks.enabled = [];

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
    hooks.useSetWikiPagePublish.mockReturnValue(makeMutation());
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

    await goToSection(user, 'Knowledge');

    // The entry text should appear once the tab renders it
    await waitFor(() => {
      expect(
        screen.getByText(/auth middleware must resolve effective tier/i),
      ).toBeInTheDocument();
    });
  });

  // The project bar is one line: Projects / name / section menu, the active
  // section's sub-tabs, then the compile and helper actions.
  it('renders the project bar on one line with a section menu', async () => {
    setProjectData({ name: 'Acme Widgets' });
    renderPage();
    const user = userEvent.setup();
    expect(screen.getByRole('link', { name: 'Projects' })).toHaveAttribute('href', '/projects');
    expect(screen.getByRole('heading', { level: 1, name: 'Acme Widgets' })).toBeInTheDocument();
    expectSection('Overview');
    expect(sectionButton()).toHaveAttribute('aria-haspopup', 'menu');
    expect(sectionButton()).toHaveAttribute('aria-expanded', 'false');
    // Details moved to Settings → About; the bar holds no dates or remote.
    expect(screen.queryByText(/^Created/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Updated/)).not.toBeInTheDocument();

    await user.click(sectionButton());
    expect(sectionButton()).toHaveAttribute('aria-expanded', 'true');
    const items = screen.getAllByRole('menuitem').map((t) => t.textContent?.trim());
    expect(items).toEqual(['Overview', 'Knowledge', 'Work', 'Rules & Skills', 'Settings']);
    expect(screen.queryByRole('menuitem', { name: 'Marketplace' })).not.toBeInTheDocument();
  });

  it('puts the pending count on the Knowledge menu item and the Entries sub-tab', async () => {
    setProjectData();
    hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 7 }, isLoading: false });
    renderPage();
    const user = userEvent.setup();
    await user.click(sectionButton());
    expect(screen.getByRole('menuitem', { name: /^Knowledge/ })).toHaveTextContent('7');
    await user.click(screen.getByRole('menuitem', { name: /^Knowledge/ }));
    expect(screen.getByRole('tab', { name: /entries/i })).toHaveTextContent('7');
  });

  it('shows sub-tabs per section and none for Overview or Settings', async () => {
    setProjectData();
    renderPage();
    const user = userEvent.setup();
    expect(screen.queryAllByRole('tab')).toHaveLength(0);

    await goToSection(user, 'Knowledge');
    expect(screen.getByRole('tablist', { name: 'Knowledge views' })).toBeInTheDocument();
    expect(screen.getAllByRole('tab').map((t) => t.textContent?.trim().replace(/\d+$/, ''))).toEqual([
      'Entries', 'Pages', 'History',
    ]);

    await goToSection(user, 'Work');
    expect(screen.getByRole('tab', { name: 'Tickets' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Agent runs' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Agents' })).toBeInTheDocument();

    await goToSection(user, 'Rules & Skills');
    expect(screen.getByRole('tab', { name: 'Rules' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Skills' })).toBeInTheDocument();

    await goToSection(user, 'Settings');
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
  });

  it('opens the helper on this project from the bar', async () => {
    setProjectData({ name: 'Acme Widgets' });
    renderPage();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Ask the helper' }));
    expect(helperOpen).toHaveBeenCalledWith({ project_id: 'proj_abc', label: 'Acme Widgets' });
  });

  it('shows the repo-reclaimed notice under the bar', () => {
    setProjectData({ repo_reclaimed_at: '2026-10-01T00:00:00Z' });
    renderPage();
    expect(screen.getByText(/all repos were reclaimed/i)).toBeInTheDocument();
  });

  describe('URL-driven tabs', () => {
    it('reads ?tab= and &sub= on load', () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?tab=knowledge&sub=history');
      expectSection('Knowledge');
      expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
    });

    it('writes the section and sub-tab to the URL on click, keeping other params', async () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?edit=1');
      const user = userEvent.setup();

      await goToSection(user, 'Work');
      expect(screen.getByTestId('loc')).toHaveTextContent('tab=work');
      expect(screen.getByTestId('loc')).toHaveTextContent('edit=1');

      await user.click(screen.getByRole('tab', { name: 'Agents' }));
      expect(screen.getByTestId('loc')).toHaveTextContent('tab=work');
      expect(screen.getByTestId('loc')).toHaveTextContent('sub=personas');

      // Changing section drops the previous section's sub-tab.
      await goToSection(user, 'Knowledge');
      expect(screen.getByTestId('loc')).not.toHaveTextContent('sub=personas');
    });

    it('drops ?run= and ?story= when leaving the tab they belong to', async () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?tab=work&sub=agent-runs&run=run_1&story=tk_1');
      const user = userEvent.setup();
      await user.click(screen.getByRole('tab', { name: 'Agents' }));
      expect(screen.getByTestId('loc')).not.toHaveTextContent('run=');
      expect(screen.getByTestId('loc')).not.toHaveTextContent('story=');
    });

    it('drops ?run= and ?story= when changing section', async () => {
      setProjectData();
      renderPage('/projects/sessionfs%2Fsessionfs?tab=work&sub=agent-runs&run=run_1&story=tk_1');
      const user = userEvent.setup();
      await goToSection(user, 'Knowledge');
      expect(screen.getByTestId('loc')).not.toHaveTextContent('run=');
      expect(screen.getByTestId('loc')).not.toHaveTextContent('story=');
    });

    it('Back returns to the previous section; sub-tab clicks do not add history', async () => {
      setProjectData();
      renderPage();
      const user = userEvent.setup();
      await goToSection(user, 'Work');
      await user.click(screen.getByRole('tab', { name: 'Agent runs' }));
      await user.click(screen.getByRole('tab', { name: 'Agents' }));
      await user.click(screen.getByRole('button', { name: 'history back' }));
      expectSection('Overview');
    });

    it('falls back to Overview and the first sub-tab for unknown values', () => {
      setProjectData();
      const { unmount } = renderPage('/projects/sessionfs%2Fsessionfs?tab=nonsense');
      expectSection('Overview');
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
      ['personas', 'Work', 'Agents'],
      ['rules', 'Rules & Skills', 'Rules'],
      ['marketplace', 'Rules & Skills', 'Skills'],
      ['repos', 'Settings', null],
      ['merge', 'Settings', null],
      ['transfer', 'Settings', null],
      ['context', 'Overview', null],
    ])('maps the legacy ?tab=%s key to %s', (legacy, section, subTab) => {
      setProjectData();
      renderPage(`/projects/sessionfs%2Fsessionfs?tab=${legacy}`);
      expectSection(section);
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

  it('shows one Compile action, in the bar, not on the Overview card', async () => {
    setProjectData();
    const compile = makeMutation();
    hooks.useCompileProject.mockReturnValue(compile);
    hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 3 }, isLoading: false });
    renderPage();
    const button = screen.getByRole('button', { name: /^compile 3 pending entries/i });
    expect(screen.getAllByRole('button', { name: /^compile/i })).toHaveLength(1);
    expect(button).toHaveTextContent('Compile 3');
    await userEvent.setup().click(button);
    expect(compile.mutate).toHaveBeenCalled();
  });

  it('offers no Compile action to a reader who cannot edit', () => {
    setProjectData({ can_edit: false });
    hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 3 }, isLoading: false });
    renderPage();
    expect(screen.queryByRole('button', { name: /^compile/i })).not.toBeInTheDocument();
  });

  it('keeps the tickets list view in the URL and maps the old ?assignee=me link to Mine', async () => {
    setProjectData();
    renderPage('/projects/proj_abc?tab=work&sub=tickets&assignee=me');
    await screen.findByText('tickets body');
    fireEvent.click(screen.getByRole('button', { name: 'view: mine' }));
    // Back to All: the default leaves the URL, and so does the legacy key.
    expect(screen.getByTestId('loc').textContent).not.toMatch(/assignee=|view=/);
    fireEvent.click(screen.getByRole('button', { name: 'view: all' }));
    expect(screen.getByTestId('loc').textContent).toMatch(/view=mine/);
    expect(screen.getByTestId('loc').textContent).toMatch(/sub=tickets/);
    fireEvent.click(screen.getByRole('button', { name: 'group: status' }));
    expect(screen.getByTestId('loc').textContent).toMatch(/group=issue/);
    expect(screen.getByTestId('loc').textContent).toMatch(/q=webhook/);
  });

  it('gives the tickets tab admin standing from the project, with an owner fallback', async () => {
    setProjectData({ owner_id: 'someone_else', can_admin: true });
    renderPage('/projects/proj_abc?tab=work&sub=tickets');
    await screen.findByText('tickets body');
    expect(ticketViewer.value?.isProjectAdmin).toBe(true);
    cleanup();
    setProjectData({ owner_id: 'someone_else', can_admin: false });
    renderPage('/projects/proj_abc?tab=work&sub=tickets');
    await screen.findByText('tickets body');
    expect(ticketViewer.value?.isProjectAdmin).toBe(false);
    cleanup();
    // An older server without can_admin: the owner still counts.
    setProjectData({ owner_id: meData.value.user_id });
    renderPage('/projects/proj_abc?tab=work&sub=tickets');
    await screen.findByText('tickets body');
    expect(ticketViewer.value?.isProjectAdmin).toBe(true);
  });

  it('maps the old ?due=overdue link to the Overdue view', async () => {
    setProjectData();
    renderPage('/projects/proj_abc?tab=work&sub=tickets&due=overdue');
    await screen.findByText('tickets body');
    expect(screen.getByRole('button', { name: 'view: overdue' })).toBeInTheDocument();
  });

  it("ignores another sub-tab's ?view= on the tickets list", async () => {
    setProjectData();
    renderPage('/projects/proj_abc?tab=work&sub=tickets&view=board');
    await screen.findByText('tickets body');
    expect(screen.getByRole('button', { name: 'view: all' })).toBeInTheDocument();
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

    it('offers no Jira surface to a reader who is not a member of the project org', async () => {
      // A captured-session reader can read the org project, but the server
      // refuses its Jira routes with 403 org_membership_required.
      setProjectData({ org_id: 'org_x', effective_tier: 'enterprise', can_edit: false });
      myOrgsData.value = [{ org_id: 'org_other', name: 'Other', role: 'admin' }];
      renderPage('/projects/proj_abc?tab=work&sub=tickets');
      await screen.findByText('tickets body');
      expect(jiraSurface.value).toBeNull();
    });
  });

  describe('Confluence context publish button', () => {
    const PUBLISH = { name: 'Publish to Confluence' };

    it('shows when the mapping publishes the context', () => {
      setProjectData({ org_id: 'org_x', effective_tier: 'enterprise', can_edit: true });
      confluenceLinks.value = { links: [], mapping: { publish_context: true } };
      renderPage();
      expect(screen.getByRole('button', PUBLISH)).toBeInTheDocument();
    });

    it('hides when the mapping does not publish the context', () => {
      setProjectData({ org_id: 'org_x', effective_tier: 'enterprise', can_edit: true });
      confluenceLinks.value = { links: [], mapping: { publish_context: false } };
      renderPage();
      expect(screen.queryByRole('button', PUBLISH)).not.toBeInTheDocument();
    });

    it('hides when the links query fails (not connected / no mapping)', () => {
      setProjectData({ org_id: 'org_x', effective_tier: 'enterprise', can_edit: true });
      confluenceLinks.value = undefined;
      renderPage();
      expect(screen.queryByRole('button', PUBLISH)).not.toBeInTheDocument();
    });

    it('does not fetch links below Enterprise', () => {
      setProjectData({ org_id: 'org_x', effective_tier: 'team', can_edit: true });
      confluenceLinks.value = { links: [], mapping: { publish_context: true } };
      renderPage();
      expect(confluenceLinks.enabled.every((e) => e === false)).toBe(true);
      expect(screen.queryByRole('button', PUBLISH)).not.toBeInTheDocument();
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
