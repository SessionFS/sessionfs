import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectDetail from '../projects/ProjectDetail';
import { axe } from '../test/axe';

// Mirrors projects/ProjectDetail.test.tsx: hooks mocked one layer up, the
// Work / Rules & Skills bodies (own suites, auth-bound) stubbed.
const { hooks } = vi.hoisted(() => ({
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
}));

vi.mock('../hooks/useProjects', () => hooks);
vi.mock('../hooks/useConfluencePublish', () => ({
  useConfluenceLinks: () => ({ data: undefined }),
}));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));
vi.mock('../helper/HelperProvider', () => ({ useHelper: () => ({ openHelper: vi.fn() }) }));
vi.mock('../transfers/useTransfers', () => ({
  useMyOrgs: () => ({ data: [], isLoading: false, error: null }),
}));
vi.mock('../hooks/useMe', () => ({
  useMe: () => ({
    data: { user_id: 'u_1', tier: 'enterprise', effective_tier: 'enterprise' },
    isLoading: false,
  }),
}));
vi.mock('../projects/TicketsTab', () => ({ default: () => <p>tickets body</p> }));
vi.mock('../projects/AgentRunsTab', () => ({ default: () => <p>agent runs body</p> }));
vi.mock('../projects/PersonasTab', () => ({ default: () => <p>personas body</p> }));
vi.mock('../projects/RulesTab', () => ({ default: () => <p>rules body</p> }));
vi.mock('../projects/MarketplaceTab', () => ({ default: () => <p>skills body</p> }));
vi.mock('../projects/TransferPanel', () => ({ default: () => <h3>Transfer ownership</h3> }));
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

const mutation = () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null });

beforeEach(() => {
  for (const h of Object.values(hooks)) h.mockReset();
  hooks.useProject.mockReturnValue({
    data: {
      id: 'proj_abc',
      git_remote_normalized: 'acme/widgets',
      context_document: '# Project\n\nSome context here.',
      created_at: '2026-03-01T12:00:00Z',
      updated_at: '2026-04-10T12:00:00Z',
      auto_narrative: false,
    },
    isLoading: false,
    error: null,
  });
  hooks.useKnowledgeEntries.mockReturnValue({ data: { entries: [], total: 0 }, isLoading: false });
  hooks.useCompileStatus.mockReturnValue({
    data: {
      last_compile_at: null,
      pending_claims: 0,
      compile_needed: false,
      compile_schedule: 'nightly',
      compile_after_claims: null,
      last_scheduled_compile_at: null,
      next_scheduled_at: null,
      compile_history: [],
    },
    isLoading: false,
  });
  hooks.useCompilations.mockReturnValue({ data: { compilations: [] }, isLoading: false });
  hooks.useWikiPages.mockReturnValue({ data: { pages: [] }, isLoading: false });
  hooks.useWikiPage.mockReturnValue({ data: null, isLoading: false });
  hooks.useProjectHealth.mockReturnValue({ data: null, isLoading: false });
  hooks.useProjectRepos.mockReturnValue({ data: [], isLoading: false, error: null });
  hooks.useProjects.mockReturnValue({ data: [], isLoading: false, error: null });
  for (const k of [
    'useDismissEntry', 'useCompileProject', 'useUpdateProjectContext', 'useDeleteProject',
    'useUpdateWikiPage', 'useSetWikiPagePublish', 'useDeleteWikiPage', 'useRegenerateWikiPage', 'useUpdateProjectSettings',
    'useDismissStaleEntries', 'usePromoteEntry', 'useSupersedeEntry', 'useUpdateProjectName',
    'useLinkRepo', 'useUnlinkRepo', 'useMergeProject', 'useRebuildProject', 'useRefreshEntry',
  ] as const) hooks[k].mockReturnValue(mutation());
});

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/projects/acme%2Fwidgets${search}`]}>
      <main>
        <Routes>
          <Route path="/projects/:id" element={<ProjectDetail />} />
        </Routes>
      </main>
    </MemoryRouter>,
  );
}

describe('ProjectDetail sections', () => {
  const cases: Array<[string, string, string]> = [
    ['Overview', '', ''],
    ['Knowledge', '?tab=knowledge', ''],
    ['Work', '?tab=work', 'tickets body'],
    ['Rules & Skills', '?tab=config', 'rules body'],
    ['Settings', '?tab=settings', 'Transfer ownership'],
  ];
  for (const [name, search, marker] of cases) {
    it(`${name} section has no axe violations`, async () => {
      const { container } = renderAt(search);
      await screen.findByRole('heading', { name: /acme\/widgets/i });
      if (marker) await screen.findByText(marker);
      expect(await axe(container)).toHaveNoViolations();
    });
  }
});
