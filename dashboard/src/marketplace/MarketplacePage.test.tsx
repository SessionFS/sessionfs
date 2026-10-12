import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MarketplacePage, { isTierGate } from './MarketplacePage';
import { ApiError } from '../api/client';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useMarketplacePackages: vi.fn(),
    useMarketplacePreview: vi.fn(),
    useCreateSkillInstallRequest: vi.fn(),
    useDeletePersonalSkillVersion: vi.fn(),
    useEffectiveSkillAssignments: vi.fn(),
    useOrgSkillControl: vi.fn(),
    useMe: vi.fn(),
    useProjects: vi.fn(),
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMarketplace', () => ({
  MARKETPLACE_TOOLS: ['claude-code', 'codex', 'gemini'],
  useMarketplacePackages: hooks.useMarketplacePackages,
  useMarketplacePreview: hooks.useMarketplacePreview,
  useCreateSkillInstallRequest: hooks.useCreateSkillInstallRequest,
  useDeletePersonalSkillVersion: hooks.useDeletePersonalSkillVersion,
  useEffectiveSkillAssignments: hooks.useEffectiveSkillAssignments,
  useOrgSkillControl: hooks.useOrgSkillControl,
}));
vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../hooks/useProjects', () => ({ useProjects: hooks.useProjects }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    auth: { client: { downloadMarketplaceVersion: vi.fn() } },
  }),
}));

const packageRow = {
  listing_id: 'sml_1',
  collection: 'curated',
  visibility: 'public',
  verification_state: 'verified',
  package_id: 'spk_1',
  namespace: 'sessionfs',
  name: 'review-helper',
  display_name: 'Review Helper',
  description: 'Reviews a change against the project contract.',
  version_id: 'skv_1',
  version: '1.2.3',
  content_hash: `sha256:${'a'.repeat(64)}`,
  source: { type: 'hosted-upload', locator: 'upload:review.tar.gz', subdir: '', commit: null },
  risk: { scripts: true, mcp: false, executable_extensions: false, instruction_only_load: true },
  compatibility: {
    codex: { native_skills: true, instruction_files: 2, omitted_components: ['scripts_require_consent'] },
  },
  license: { declared: true, value: 'Apache-2.0' },
  signature: {},
  blob_size_bytes: 1024,
  published_at: '2026-09-01T00:00:00Z',
};

function pages(items: unknown[], extra: Record<string, unknown> = {}) {
  return {
    data: { pages: [{ items, count: items.length, has_more: false, next_cursor: null }], pageParams: [''] },
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    ...extra,
  };
}

beforeEach(() => {
  hooks.useMarketplacePackages.mockReturnValue(pages([packageRow]));
  hooks.useMarketplacePreview.mockReturnValue({
    data: { instruction_preview: [{ path: 'skills/review/SKILL.md', content: '# Review contract' }] },
    isLoading: false,
    error: null,
  });
  hooks.useCreateSkillInstallRequest.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  hooks.useDeletePersonalSkillVersion.mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue(undefined), isPending: false });
  hooks.useEffectiveSkillAssignments.mockReturnValue({ data: { items: [] }, isLoading: false });
  hooks.useOrgSkillControl.mockReturnValue({
    registries: { data: { items: [] }, isLoading: false },
    assignments: { data: { items: [] }, isLoading: false },
  });
  hooks.useMe.mockReturnValue({ data: { org_id: null, user_id: 'user_1' } });
  hooks.useProjects.mockReturnValue({ data: [], isLoading: false });
});

describe('MarketplacePage', () => {
  it('renders discoverable package trust state', () => {
    render(<MarketplacePage />);
    expect(screen.getByRole('heading', { name: /Portable agent capabilities/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Inspect Review Helper' })).toBeInTheDocument();
    expect(screen.getByText('verified')).toBeInTheDocument();
    expect(screen.getByText(/1 elevated capability flag/)).toBeInTheDocument();
  });

  it('shows provenance, license, degradation, and a visibly untrusted preview', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: 'Inspect Review Helper' }));
    expect(screen.getByText('Apache-2.0')).toBeInTheDocument();
    expect(screen.getByText(/upload:review.tar.gz/)).toBeInTheDocument();
    expect(screen.getByText('scripts_require_consent')).toBeInTheDocument();
    expect(screen.getByText('Present — explicit consent')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Preview instructions' }));
    expect(screen.getByRole('heading', { name: 'Untrusted instruction preview' })).toBeInTheDocument();
    expect(screen.getByText('# Review contract')).toBeInTheDocument();
  });

  it('surfaces frontmatter hooks and pre-approved tools in the trust dossier', async () => {
    const user = userEvent.setup();
    hooks.useMarketplacePackages.mockReturnValue(pages([{
      ...packageRow,
      risk: { ...packageRow.risk, hooks: true, hook_events: ['PreToolUse'], allowed_tools: ['allowed-tools:Bash'] },
    }]));
    render(<MarketplacePage />);
    expect(screen.getByText(/3 elevated capability flags/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Inspect Review Helper' }));
    expect(screen.getByText('Frontmatter hooks')).toBeInTheDocument();
    expect(screen.getByText('PreToolUse')).toBeInTheDocument();
    expect(screen.getByText('Pre-approved tools')).toBeInTheDocument();
    expect(screen.getByText('allowed-tools:Bash')).toBeInTheDocument();
    expect(screen.getAllByText('Present — explicit consent')).toHaveLength(3);
  });

  it('lets the owner delete their own private version behind a confirmation', async () => {
    const user = userEvent.setup();
    const mutateAsync = vi.fn().mockResolvedValue(undefined);
    hooks.useDeletePersonalSkillVersion.mockReturnValue({ mutateAsync, isPending: false });
    hooks.useMarketplacePackages.mockReturnValue(pages([{ ...packageRow, visibility: 'user', owner_user_id: 'user_1' }]));
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: 'Inspect Review Helper' }));
    await user.click(screen.getByRole('button', { name: 'Delete version…' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Delete version' }));
    expect(mutateAsync).toHaveBeenCalledWith({ packageId: 'spk_1', versionId: 'skv_1' });
  });

  it('hides the delete action on packages the viewer does not own', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: 'Inspect Review Helper' }));
    expect(screen.queryByRole('button', { name: 'Delete version…' })).not.toBeInTheDocument();
  });

  it('renders declared behavior frontmatter in the trust dossier', async () => {
    const user = userEvent.setup();
    hooks.useMarketplacePackages.mockReturnValue(pages([{
      ...packageRow,
      risk: { ...packageRow.risk, disclosed_metadata: { 'review-helper': { model: 'opus', effort: 'high', shell: 'bash' } } },
    }]));
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: 'Inspect Review Helper' }));
    expect(screen.getByRole('heading', { name: 'Declared behavior' })).toBeInTheDocument();
    expect(screen.getByText('review-helper · model')).toBeInTheDocument();
    expect(screen.getByText('opus')).toBeInTheDocument();
    expect(screen.getByText('bash')).toBeInTheDocument();
  });

  it('omits the declared behavior block when nothing is declared', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: 'Inspect Review Helper' }));
    expect(screen.queryByRole('heading', { name: 'Declared behavior' })).not.toBeInTheDocument();
  });

  it('offers Load more only while the server reports another page', async () => {
    const user = userEvent.setup();
    const fetchNextPage = vi.fn();
    hooks.useMarketplacePackages.mockReturnValue(pages([packageRow, { ...packageRow, listing_id: 'sml_2', display_name: 'Second Helper' }], {
      hasNextPage: true,
      fetchNextPage,
    }));
    const first = render(<MarketplacePage />);
    expect(screen.getByText('2 packages loaded')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    expect(fetchNextPage).toHaveBeenCalledTimes(1);
    first.unmount();

    hooks.useMarketplacePackages.mockReturnValue(pages([packageRow]));
    render(<MarketplacePage />);
    expect(screen.getByText('1 package')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });

  it('surfaces the Team tier gate on organization registries instead of "none configured"', () => {
    hooks.useMe.mockReturnValue({ data: { org_id: 'org_1', user_id: 'user_1' } });
    hooks.useOrgSkillControl.mockReturnValue({
      registries: { data: undefined, isLoading: false, error: new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'Team tier required' } })) },
      assignments: { data: { items: [] }, isLoading: false },
    });
    render(<MarketplacePage />);
    expect(screen.getByText('Upgrade to Team')).toBeInTheDocument();
    expect(screen.queryByText('No organization registries configured.')).not.toBeInTheDocument();
    expect(isTierGate(new ApiError(403, JSON.stringify({ error: { code: 'feature_not_available', message: 'x' } })))).toBe(true);
    expect(isTierGate(new ApiError(500, 'boom'))).toBe(false);
  });

  it('renders the authorized empty state', () => {
    hooks.useMarketplacePackages.mockReturnValue(pages([]));
    render(<MarketplacePage />);
    expect(screen.getByRole('heading', { name: 'No packages match' })).toBeInTheDocument();
    expect(screen.getByText(/Private organization packages/)).toBeInTheDocument();
  });
});
