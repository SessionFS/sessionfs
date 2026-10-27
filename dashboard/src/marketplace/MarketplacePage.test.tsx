import { render as rtlRender, screen, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MarketplacePage, { isTierGate } from './MarketplacePage';
import { ApiError } from '../api/client';
import { axe } from '../test/axe';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useMarketplacePackages: vi.fn(),
    useMarketplacePreview: vi.fn(),
    useMarketplaceCategories: vi.fn(),
    useMarketplaceReadme: vi.fn(),
    useCreateSkillInstallRequest: vi.fn(),
    useDeletePersonalSkillVersion: vi.fn(),
    useEffectiveSkillAssignments: vi.fn(),
    useOrgSkillControl: vi.fn(),
    useMe: vi.fn(),
    useProjects: vi.fn(),
    useRevocationIndex: vi.fn(),
    useMyOrgs: vi.fn(),
    usePublishSkillPackage: vi.fn(),
    useReportMarketplacePackage: vi.fn(),
    useRequestRegistryPoll: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
    addToast: vi.fn(),
    useMarketplaceDisclosure: vi.fn((_versionId?: string, _tools?: string[]) => ({ data: undefined as unknown, isLoading: false, error: null })),
    useHeldUpdateCounts: vi.fn((...args: unknown[]) => { void args; return { total: 0, more: false, byOrg: {} as Record<string, number>, errors: {} as Record<string, unknown>, loaded: false }; }),
  },
}));

function render(ui: ReactElement) {
  return rtlRender(<MemoryRouter>{ui}</MemoryRouter>);
}

vi.mock('../hooks/useMarketplace', () => ({
  MARKETPLACE_TOOLS: ['claude-code', 'codex', 'gemini'],
  useMarketplacePackages: hooks.useMarketplacePackages,
  useMarketplacePreview: hooks.useMarketplacePreview,
  useMarketplaceCategories: hooks.useMarketplaceCategories,
  useMarketplaceReadme: hooks.useMarketplaceReadme,
  useCreateSkillInstallRequest: hooks.useCreateSkillInstallRequest,
  useDeletePersonalSkillVersion: hooks.useDeletePersonalSkillVersion,
  useEffectiveSkillAssignments: hooks.useEffectiveSkillAssignments,
  useOrgSkillControl: hooks.useOrgSkillControl,
  useRevocationIndex: hooks.useRevocationIndex,
  usePublishSkillPackage: hooks.usePublishSkillPackage,
  useReportMarketplacePackage: hooks.useReportMarketplacePackage,
  useRequestRegistryPoll: hooks.useRequestRegistryPoll,
}));
vi.mock('../transfers/useTransfers', () => ({ useMyOrgs: hooks.useMyOrgs }));
vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../hooks/useMarketplaceDisclosure', () => ({
  useMarketplaceDisclosure: (versionId?: string, tools?: string[]) => hooks.useMarketplaceDisclosure(versionId, tools),
  useHeldUpdateCounts: (orgIds: string[]) => hooks.useHeldUpdateCounts(orgIds),
}));
vi.mock('../hooks/useProjects', () => ({ useProjects: hooks.useProjects }));
vi.mock('../hooks/useNativeCatalogs', () => ({
  useNativeCatalogs: () => ({ data: undefined, isLoading: false, error: null }),
  useWorksIn: () => ({ data: undefined, isLoading: false, error: null }),
}));
vi.mock('./HeldUpdates', () => ({
  HeldUpdateQueues: ({ orgs, value }: { orgs: { org_id: string }[]; value?: string }) => (
    <div data-testid="held-queues" data-value={value}>{orgs.map((org) => org.org_id).join(',')}</div>
  ),
}));
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
  moderation_state: 'approved',
  publisher_id: 'pub_sessionfs',
  publisher: {
    id: 'pub_sessionfs', namespace: 'sessionfs', display_name: 'SessionFS', owner_type: 'system',
    status: 'active', verified: true, badges: ['verified: SessionFS platform'],
  },
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
  hooks.useMarketplaceDisclosure.mockReturnValue({ data: undefined, isLoading: false, error: null });
  hooks.useHeldUpdateCounts.mockReturnValue({ total: 0, more: false, byOrg: {}, errors: {}, loaded: false });
  hooks.useMarketplacePackages.mockReturnValue(pages([packageRow]));
  hooks.useMarketplacePreview.mockReturnValue({
    data: { instruction_preview: [{ path: 'skills/review/SKILL.md', content: '# Review contract' }] },
    isLoading: false,
    error: null,
  });
  hooks.useMarketplaceCategories.mockReturnValue({ data: { items: [
    { slug: 'review', display_name: 'Code review' }, { slug: 'testing', display_name: 'Testing' },
  ] } });
  hooks.useMarketplaceReadme.mockReturnValue({ data: undefined, isLoading: false, error: null });
  hooks.useCreateSkillInstallRequest.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  hooks.useDeletePersonalSkillVersion.mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue(undefined), isPending: false });
  hooks.useEffectiveSkillAssignments.mockReturnValue({ data: { items: [] }, isLoading: false });
  hooks.useOrgSkillControl.mockReturnValue({
    registries: { data: { items: [] }, isLoading: false },
    assignments: { data: { items: [] }, isLoading: false },
  });
  hooks.useMe.mockReturnValue({ data: { org_id: null, user_id: 'user_1' } });
  hooks.useProjects.mockReturnValue({ data: [], isLoading: false });
  hooks.useRevocationIndex.mockReturnValue({ data: undefined });
  hooks.useMyOrgs.mockReturnValue({ data: [], isLoading: false });
  hooks.usePublishSkillPackage.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  hooks.useReportMarketplacePackage.mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue({ id: 'spr_1' }), isPending: false });
});

describe('MarketplacePage', () => {
  it('renders discoverable package trust state', () => {
    render(<MarketplacePage />);
    expect(screen.getByRole('heading', { level: 1, name: 'Marketplace' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Review Helper/ })).toBeInTheDocument();
    // Publisher identity, not "a platform admin published it" (marketplace v2 §4.3):
    // a check mark whose tooltip carries the server-derived badge text.
    // The check mark is decorative; the title button carries the words.
    expect(screen.getByTestId('verified-mark')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByRole('button', { name: 'Review Helper, verified publisher' })).toBeInTheDocument();
    expect(screen.queryByText('verified')).toBeNull();
    expect(screen.getByTestId('capability-summary')).toHaveTextContent('Includes scripts');
  });

  it('keeps cards quiet: one interactive element, no public pill, no dangling separator', () => {
    hooks.useMarketplacePackages.mockReturnValue(pages([
      packageRow,
      { ...packageRow, listing_id: 'sml_2', display_name: 'Org Helper', visibility: 'org', installs: undefined },
    ]));
    render(<MarketplacePage />);
    const cards = screen.getAllByRole('article');
    expect(cards).toHaveLength(2);
    for (const card of cards) {
      // The title button is the only control; its ::after overlay makes the card clickable.
      expect(within(card).getAllByRole('button')).toHaveLength(1);
      expect(within(card).queryByRole('link')).toBeNull();
      const meta = card.lastElementChild as HTMLElement;
      expect(meta.textContent?.trim().endsWith('·')).toBe(false);
    }
    expect(within(cards[0]).queryByText('Public')).toBeNull();
    expect(within(cards[0]).queryByText('public')).toBeNull();
    // Visibility sits in the footer meta line, not in the namespace row.
    // Visibility sits in the namespace row as plain text; the footer stays one line.
    expect(within(cards[1]).getByTestId('card-visibility')).toHaveTextContent('Organization only');
    expect(within(cards[1].lastElementChild as HTMLElement).queryByText('Organization only')).toBeNull();
    expect(within(cards[1]).getByText('— installs')).toBeInTheDocument();
  });

  it('opens the package from the card title', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByRole('dialog', { name: 'Review Helper' })).toBeInTheDocument();
  });

  it('shows provenance, license, degradation, and a visibly untrusted preview', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getAllByText('Apache-2.0').length).toBeGreaterThan(0);
    expect(screen.getByText(/upload:review.tar.gz/)).toBeInTheDocument();
    expect(screen.getByText('Asks for consent')).toBeInTheDocument();
    expect(screen.getByText(/Not included: MCP servers/)).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Compatibility' }));
    expect(screen.getByText('scripts_require_consent')).toBeInTheDocument();
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
    expect(screen.getByTestId('capability-summary')).toHaveTextContent('Includes scripts +2');
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByText('Hooks')).toBeInTheDocument();
    expect(screen.getByText('PreToolUse')).toBeInTheDocument();
    expect(screen.getByText('Pre-approved tools')).toBeInTheDocument();
    expect(screen.getByText('allowed-tools:Bash')).toBeInTheDocument();
    expect(screen.getAllByText('Asks for consent')).toHaveLength(3);
  });

  it('lets the owner delete their own private version behind a confirmation', async () => {
    const user = userEvent.setup();
    const mutateAsync = vi.fn().mockResolvedValue(undefined);
    hooks.useDeletePersonalSkillVersion.mockReturnValue({ mutateAsync, isPending: false });
    hooks.useMarketplacePackages.mockReturnValue(pages([{ ...packageRow, visibility: 'user', owner_user_id: 'user_1' }]));
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    await user.click(screen.getByRole('button', { name: 'Delete version…' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Delete version' }));
    expect(mutateAsync).toHaveBeenCalledWith({ packageId: 'spk_1', versionId: 'skv_1' });
  });

  it('hides the delete action on packages the viewer does not own', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.queryByRole('button', { name: 'Delete version…' })).not.toBeInTheDocument();
  });

  it('renders declared behavior frontmatter in the trust dossier', async () => {
    const user = userEvent.setup();
    hooks.useMarketplacePackages.mockReturnValue(pages([{
      ...packageRow,
      risk: { ...packageRow.risk, disclosed_metadata: { 'review-helper': { model: 'opus', effort: 'high', shell: 'bash' } } },
    }]));
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByRole('heading', { name: 'Declared behavior' })).toBeInTheDocument();
    expect(screen.getByText('review-helper · model')).toBeInTheDocument();
    expect(screen.getByText('opus')).toBeInTheDocument();
    expect(screen.getByText('bash')).toBeInTheDocument();
  });

  it('omits the declared behavior block when nothing is declared', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
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
    expect(screen.getByTestId('package-count')).toHaveTextContent('2 loaded');
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    expect(fetchNextPage).toHaveBeenCalledTimes(1);
    first.unmount();

    hooks.useMarketplacePackages.mockReturnValue(pages([packageRow]));
    render(<MarketplacePage />);
    expect(screen.getByTestId('package-count')).toHaveTextContent(/^1$/);
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
    expect(screen.queryByText('No registries configured.')).not.toBeInTheDocument();
    // Only the registries part is gated; the viewer's assignments still show.
    expect(screen.getByRole('heading', { name: /Assigned to you/ })).toBeInTheDocument();
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

function apiError(status: number, error: Record<string, unknown>) {
  return new ApiError(status, JSON.stringify({ error }));
}

describe('MarketplacePage publishing and moderation entry points', () => {
  it('hides Publish for a free user with no administered org and explains the upgrade', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'free', effective_tier: 'free', org_id: null } });
    render(<MarketplacePage />);
    expect(screen.queryByRole('button', { name: 'Publish package' })).not.toBeInTheDocument();
    expect(screen.getByText(/Publishing needs the Starter plan/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View plans' })).toHaveAttribute('href', '/settings/billing');
  });

  it('offers Publish to a Starter+ user and to a free user who owns an org', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'pro', effective_tier: 'pro' } });
    const first = render(<MarketplacePage />);
    expect(screen.getByRole('button', { name: 'Publish package' })).toBeInTheDocument();
    first.unmount();

    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'free', effective_tier: 'free' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'owner' }], isLoading: false });
    render(<MarketplacePage />);
    expect(screen.getByRole('button', { name: 'Publish package' })).toBeInTheDocument();
  });

  it('shows held-update queues for every org the viewer owns or administers', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'team', org_id: 'org_1', org_role: 'member' } });
    hooks.useOrgSkillControl.mockReturnValue({ registries: { data: { items: [] }, isLoading: false, error: null } });
    hooks.useMyOrgs.mockReturnValue({
      data: [
        { org_id: 'org_1', name: 'Primary', role: 'member' },
        { org_id: 'org_2', name: 'Second', role: 'admin' },
        { org_id: 'org_3', name: 'Third', role: 'owner' },
      ],
      isLoading: false,
    });
    hooks.useHeldUpdateCounts.mockReturnValue({ total: 2, more: false, byOrg: { org_2: 1, org_3: 1 }, errors: {}, loaded: true });
    const first = render(<MarketplacePage />);
    // Counts are asked for administered orgs only, never the member org.
    expect(hooks.useHeldUpdateCounts).toHaveBeenLastCalledWith(['org_2', 'org_3']);
    // Not the primary org (member there), but both administered orgs.
    expect(screen.getByTestId('held-queues')).toHaveTextContent('org_2,org_3');
    first.unmount();

    // Administering an org is enough even without a primary org.
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'free' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_9', name: 'Other', role: 'admin' }], isLoading: false });
    hooks.useHeldUpdateCounts.mockReturnValue({ total: 1, more: false, byOrg: { org_9: 1 }, errors: {}, loaded: true });
    render(<MarketplacePage />);
    expect(screen.getByTestId('held-queues')).toHaveTextContent('org_9');
  });

  it('shows the shared sub-nav, with Moderation and Usage only for org owners/admins', () => {
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'owner' }], isLoading: false });
    const first = render(<MarketplacePage />);
    const nav = screen.getByRole('navigation', { name: 'Marketplace sections' });
    expect(within(nav).getByRole('link', { name: 'Moderation' })).toHaveAttribute('href', '/marketplace/moderation');
    expect(within(nav).getByRole('link', { name: 'Usage' })).toHaveAttribute('href', '/marketplace/usage');
    first.unmount();

    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'member' }], isLoading: false });
    render(<MarketplacePage />);
    const memberNav = screen.getByRole('navigation', { name: 'Marketplace sections' });
    expect(within(memberNav).queryByRole('link', { name: 'Moderation' })).not.toBeInTheDocument();
    expect(within(memberNav).queryByRole('link', { name: 'Usage' })).not.toBeInTheDocument();
  });

  it('badges a yanked version from the revocations feed and blocks download and requests', async () => {
    const user = userEvent.setup();
    hooks.useRevocationIndex.mockReturnValue({
      data: { packages: new Map(), versions: new Map([['skv_1', 'yanked']]) },
    });
    render(<MarketplacePage />);
    expect(screen.getByText('yanked')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByText('This version is yanked.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download archive' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Request for project' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Preview instructions' })).toBeDisabled();
  });

  it('treats a package-wide block as blocking every version', async () => {
    const user = userEvent.setup();
    hooks.useRevocationIndex.mockReturnValue({
      data: { packages: new Map([['spk_1', 'blocked']]), versions: new Map() },
    });
    render(<MarketplacePage />);
    expect(screen.getByText('blocked')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByText('This version is blocked.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download archive' })).toBeDisabled();
  });

  it('badges a listing the server marks blocked even without feed data', () => {
    hooks.useMarketplacePackages.mockReturnValue(pages([{ ...packageRow, verification_state: 'blocked' }]));
    render(<MarketplacePage />);
    expect(screen.getByText('blocked')).toBeInTheDocument();
  });
});

describe('Report dialog', () => {
  async function openReport() {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    await user.click(screen.getByRole('button', { name: 'Report…' }));
    return user;
  }

  it('files a version-scoped report with a reason and details', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ id: 'spr_1' });
    hooks.useReportMarketplacePackage.mockReturnValue({ mutateAsync, isPending: false });
    const user = await openReport();
    const submit = screen.getByRole('button', { name: 'Submit report' });
    expect(submit).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /Reason/ }));
    await user.click(screen.getByRole('option', { name: /Leaks secrets/ }));
    await user.type(screen.getByLabelText('Details (optional)'), 'API key in skills/x/SKILL.md');
    await user.click(submit);
    expect(mutateAsync).toHaveBeenCalledWith({
      packageId: 'spk_1',
      reason: 'secrets',
      details: 'API key in skills/x/SKILL.md',
      version_id: 'skv_1',
    });
    expect(hooks.addToast).toHaveBeenCalledWith('success', expect.stringMatching(/Report filed/));
  });

  it('reports the whole package when the version box is unchecked', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ id: 'spr_1' });
    hooks.useReportMarketplacePackage.mockReturnValue({ mutateAsync, isPending: false });
    const user = await openReport();
    await user.click(screen.getByRole('button', { name: /Reason/ }));
    await user.click(screen.getByRole('option', { name: /Spam/ }));
    await user.click(screen.getByRole('checkbox', { name: /Only version/ }));
    await user.click(screen.getByRole('button', { name: 'Submit report' }));
    expect(mutateAsync).toHaveBeenCalledWith({ packageId: 'spk_1', reason: 'spam', details: undefined });
  });

  it('caps details at 2000 characters', async () => {
    const user = await openReport();
    const box = screen.getByLabelText('Details (optional)');
    expect(box).toHaveAttribute('maxLength', '2000');
    await user.click(box);
    await user.paste('x'.repeat(2100));
    expect((box as HTMLTextAreaElement).value).toHaveLength(2000);
    expect(screen.getByText('2000 / 2000')).toBeInTheDocument();
  });

  it('explains 409 report_already_open in plain language', async () => {
    hooks.useReportMarketplacePackage.mockReturnValue({
      mutateAsync: vi.fn().mockRejectedValue(apiError(409, { code: 'report_already_open', message: 'You already have an open report for this package.', details: { report_id: 'spr_9' } })),
      isPending: false,
    });
    const user = await openReport();
    await user.click(screen.getByRole('button', { name: /Reason/ }));
    await user.click(screen.getByRole('option', { name: /Broken/ }));
    await user.click(screen.getByRole('button', { name: 'Submit report' }));
    expect(screen.getByText('You already have an open report for this package.')).toBeInTheDocument();
    expect(screen.getByText(/Moderators will review it/)).toBeInTheDocument();
    expect(screen.queryByText(/"error"/)).not.toBeInTheDocument();
  });

  it('explains 429 rate limiting', async () => {
    hooks.useReportMarketplacePackage.mockReturnValue({
      mutateAsync: vi.fn().mockRejectedValue(apiError(429, { code: 'report_rate_limited', message: 'Too many reports. Try again later.', details: {} })),
      isPending: false,
    });
    const user = await openReport();
    await user.click(screen.getByRole('button', { name: /Reason/ }));
    await user.click(screen.getByRole('option', { name: /Something else/ }));
    await user.click(screen.getByRole('button', { name: 'Submit report' }));
    expect(screen.getByText('Too many reports right now.')).toBeInTheDocument();
    expect(screen.getByText(/20 a day/)).toBeInTheDocument();
  });
});

describe('MarketplacePage discovery (marketplace v2 §9)', () => {
  const discovered = {
    ...packageRow,
    categories: ['review'],
    installs: { window_days: 30, total: { count: null, display: '<10' }, by_tool: { codex: { count: null, display: '<10' } } },
    freshness: {
      stale: true, unmaintained: false, deprecated: true, last_updated_at: '2026-01-01T00:00:00Z',
      computed_at: '2026-10-20T00:00:00Z',
      tools: { codex: { last_updated_at: '2026-01-01T00:00:00Z', last_verified_at: '2026-02-01T00:00:00Z', verify_status: 'regressed', stale: true } },
    },
    deprecation: {
      deprecated_at: '2026-10-01T00:00:00Z', message: 'Use review-helper-2',
      successor: { package_id: 'spk_2', namespace: 'sessionfs', name: 'review-helper-2', display_name: 'Review Helper 2' },
    },
  };

  it('passes category and filter rail choices to the search hook', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    expect(hooks.useMarketplacePackages).toHaveBeenLastCalledWith('', '', { sort: 'relevance' });
    await user.click(screen.getByRole('button', { name: 'Code review' }));
    expect(hooks.useMarketplacePackages).toHaveBeenLastCalledWith('', '', { sort: 'relevance', category: 'review' });
    await user.click(screen.getByRole('button', { name: 'Filters' }));
    await user.click(screen.getByLabelText('Verified publishers only'));
    await user.click(screen.getByLabelText('Hide deprecated packages'));
    expect(hooks.useMarketplacePackages).toHaveBeenLastCalledWith('', '', {
      sort: 'relevance', category: 'review', verified: true, include_deprecated: false,
    });
    await user.click(screen.getByRole('button', { name: 'All' }));
    expect(hooks.useMarketplacePackages.mock.lastCall?.[2].category).toBeUndefined();
  });

  it('badges stale and deprecated packages and shows floored installs', () => {
    hooks.useMarketplacePackages.mockReturnValue(pages([discovered]));
    render(<MarketplacePage />);
    expect(screen.getByText('deprecated')).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.getByText('<10 installs')).toBeInTheDocument();
  });

  it('shows deprecation, per-tool freshness and a README tab in the dossier', async () => {
    const user = userEvent.setup();
    hooks.useMarketplacePackages.mockReturnValue(pages([discovered]));
    hooks.useMarketplaceReadme.mockReturnValue({
      data: {
        version_id: 'skv_1', package_id: 'spk_1', source: 'readme', path: 'README.md',
        content: '# Hello\n\n[bad](javascript:alert(1)) and [good](https://example.com)\n\n<script>alert(1)</script>',
        hidden_unicode: [], truncated: false, untrusted: true, image_path_prefix: '',
      },
      isLoading: false,
      error: null,
    });
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByText('Deprecated by its publisher.')).toBeInTheDocument();
    expect(screen.getByText('sessionfs/review-helper-2')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Compatibility' }));
    expect(screen.getByRole('heading', { name: 'Installs and freshness by tool' })).toBeInTheDocument();
    expect(screen.getByText(/regressed · stale/)).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'README' }));
    expect(hooks.useMarketplaceReadme).toHaveBeenLastCalledWith('spk_1', 'skv_1', true);
    const readme = screen.getByTestId('safe-readme');
    expect(readme.querySelector('script')).toBeNull();
    expect(readme.querySelectorAll('a')).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'good' })).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(screen.getByText('bad').tagName).toBe('SPAN');
  });
});

describe('MarketplacePage filters menu', () => {
  const lastFilters = () => hooks.useMarketplacePackages.mock.lastCall?.[2];

  it('keeps only search, tool, sort and Filters on the page and maps every panel filter to the same params', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    expect(screen.queryByRole('dialog', { name: 'Filters' })).toBeNull();
    expect(screen.queryByLabelText('Verified publishers only')).not.toBeVisible();
    await user.type(screen.getByRole('searchbox', { name: 'Search marketplace' }), 'review');
    expect(hooks.useMarketplacePackages.mock.lastCall?.[0]).toBe('review');
    await user.click(screen.getByRole('button', { name: 'Filter marketplace by tool' }));
    await user.click(screen.getByRole('option', { name: 'codex' }));
    expect(hooks.useMarketplacePackages.mock.lastCall?.[1]).toBe('codex');
    await user.click(screen.getByRole('button', { name: 'Sort' }));
    await user.click(screen.getByRole('option', { name: 'Newest' }));
    expect(lastFilters()).toEqual({ sort: 'recent' });

    const trigger = screen.getByRole('button', { name: 'Filters' });
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const panel = screen.getByRole('dialog', { name: 'Filters' });
    await user.click(within(panel).getByRole('button', { name: /Scanner findings/ }));
    await user.click(within(panel).getByRole('option', { name: 'No open findings' }));
    await user.click(within(panel).getByRole('button', { name: /Includes/ }));
    await user.click(within(panel).getByRole('option', { name: 'Scripts' }));
    await user.click(within(panel).getByRole('button', { name: /Works in/ }));
    await user.click(within(panel).getByRole('option', { name: 'gemini' }));
    await user.click(within(panel).getByRole('button', { name: /Last updated/ }));
    await user.click(within(panel).getByRole('option', { name: 'Past 30 days' }));
    await user.type(within(panel).getByLabelText('License'), 'MIT{Enter}');
    await user.click(within(panel).getByLabelText(/Published from CI only/));
    expect(lastFilters()).toEqual({
      sort: 'recent', max_severity: 'none', capability: 'scripts', works_in: 'gemini', updated_within: 30, license: 'MIT', attested: true,
    });
    expect(screen.getByRole('button', { name: /Filters/ })).toHaveTextContent('6');
  });

  it('shows active filters as removable chips with Clear all', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: 'Filters' }));
    await user.click(screen.getByLabelText('Verified publishers only'));
    await user.click(screen.getByLabelText('Hide deprecated packages'));
    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByRole('dialog', { name: 'Filters' })).toBeNull();
    const chips = screen.getByRole('group', { name: 'Active filters' });
    await user.click(within(chips).getByRole('button', { name: 'Remove filter: Verified publishers' }));
    expect(lastFilters()).toEqual({ sort: 'relevance', include_deprecated: false });
    await user.click(within(chips).getByRole('button', { name: 'Clear all' }));
    expect(lastFilters()).toEqual({ sort: 'relevance' });
    expect(screen.queryByRole('group', { name: 'Active filters' })).toBeNull();
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    const trigger = screen.getByRole('button', { name: 'Filters' });
    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: 'Filters' })).toBeVisible();
    await user.keyboard('{Escape}');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).toHaveFocus();
  });
});

describe('MarketplacePage organization library', () => {
  it('collapses to one quiet line when nothing is assigned and no registry exists', () => {
    hooks.useMe.mockReturnValue({ data: { org_id: 'org_1', user_id: 'user_1' } });
    render(<MarketplacePage />);
    expect(screen.getByText(/hasn.t assigned any packages to you/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'How organization libraries work' })).toHaveAttribute('href', expect.stringContaining('sessionfs.dev/skills-marketplace/'));
    expect(screen.queryByRole('heading', { name: /Assigned to you/ })).toBeNull();
    expect(screen.queryByText(/0 effective assignments/)).toBeNull();
  });

  it('lists assignments and registries without nested cards when there is content', () => {
    hooks.useMe.mockReturnValue({ data: { org_id: 'org_1', user_id: 'user_1' } });
    hooks.useEffectiveSkillAssignments.mockReturnValue({
      data: { items: [{ id: 'asg_1', org_id: 'org_1', version_id: 'skv_9', content_hash: 'x', target_type: 'org', target_id: 'org_1', target_user_id: null, mode: 'required', target_tools: ['codex'], policy: {}, created_at: '' }] },
      isLoading: false,
    });
    hooks.useOrgSkillControl.mockReturnValue({
      registries: { data: { items: [{ id: 'reg_1', alias: 'acme-skills', source_url: 'https://example.test/x.git', policy_state: 'approved', server_fetch_enabled: false }] }, isLoading: false },
    });
    render(<MarketplacePage />);
    expect(screen.getByRole('heading', { name: 'From your organization' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Assigned to you/ })).toBeInTheDocument();
    expect(screen.getByText('skv_9')).toBeInTheDocument();
    expect(screen.getByText('Required')).toBeInTheDocument();
    expect(screen.getByText('acme-skills')).toBeInTheDocument();
  });
});

describe('MarketplacePage accessibility', () => {
  it('browse page has no axe violations', async () => {
    const { container } = render(<main><MarketplacePage /></main>);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('open filters menu has no axe violations', async () => {
    const user = userEvent.setup();
    const { container } = render(<main><MarketplacePage /></main>);
    await user.click(screen.getByRole('button', { name: 'Filters' }));
    expect(await axe(container)).toHaveNoViolations();
  });

  it('package detail has no axe violations', async () => {
    const user = userEvent.setup();
    hooks.useMarketplacePackages.mockReturnValue(pages([{
      ...packageRow,
      risk: { ...packageRow.risk, hooks: true, hook_events: ['PreToolUse'] },
    }]));
    render(<main><MarketplacePage /></main>);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(await axe(document.body)).toHaveNoViolations();
    await user.click(screen.getByRole('tab', { name: 'Compatibility' }));
    expect(await axe(document.body)).toHaveNoViolations();
  });
});

const disclosureCard = {
  version_id: 'skv_1', content_hash: 'sha256:x', capability_digest: 'sha256:c', capabilities: ['instructions', 'scripts'],
  provenance: { publisher_verified: true, attested: false },
  scan: { ruleset: '2026.10.6', max_severity: 'critical', finding_count: 2, counts: { critical: 1, low: 1 } },
  unicode: { flagged_files: 1, findings: [{ path: 'skills/x/SKILL.md', line: 3, column: 1, rule_id: 'bidi', class: 'bidi', severity: 'high', visible: '' }], counts: { bidi: 1 } },
  incomplete: ['scripts/huge.bin'],
  shell_commands: [], network_endpoints: [], env_names: [],
  mcp_servers: { local: [], remote: [], declared: false },
  allowed_tools: [], persistence_targets: [], executable_extensions: [],
  tools: {}, consent_required: ['instructions', 'skill-scripts', 'hidden-unicode'],
};

describe('MarketplacePage package trust signals', () => {
  it('fetches the disclosure on open and shows the scanner result and alerts on Overview', async () => {
    const user = userEvent.setup();
    hooks.useMarketplaceDisclosure.mockReturnValue({ data: disclosureCard, isLoading: false, error: null });
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(hooks.useMarketplaceDisclosure).toHaveBeenCalledWith('skv_1', []);
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    const summary = screen.getByTestId('trust-summary');
    expect(summary).toHaveTextContent('ruleset 2026.10.6: 2 findings, worst critical');
    expect(within(summary).getByTestId('trust-alert-unicode')).toHaveTextContent('Hidden characters found in 1 file');
    expect(within(summary).getByTestId('trust-alert-incomplete')).toHaveTextContent('scripts/huge.bin');
    expect(within(summary).getByTestId('trust-alert-scan')).toHaveTextContent('critical');
    expect(within(summary).getByTestId('trust-alert-consent')).toHaveTextContent('scripts, hidden characters');
    expect(within(summary).getAllByRole('alert').length).toBeGreaterThanOrEqual(3);
    // The Install details tab is marked while alerts exist, and holds the full card.
    const installTab = screen.getByRole('tab', { name: /Install details/ });
    expect(installTab).toHaveTextContent('(4 alerts)');
    await user.click(installTab);
    expect(screen.getByTestId('disclosure-card')).toBeInTheDocument();
    expect(screen.getByTestId('disclosure-unicode')).toBeInTheDocument();
    expect(screen.queryByTestId('trust-summary')).toBeNull();
  });

  it('shows a calm scanner line and no alert marker for a clean package', async () => {
    const user = userEvent.setup();
    hooks.useMarketplaceDisclosure.mockReturnValue({
      data: { ...disclosureCard, scan: { ruleset: '2026.10.6', finding_count: 0 }, unicode: { flagged_files: 0, findings: [], counts: {} }, incomplete: [], consent_required: ['instructions'] },
      isLoading: false,
      error: null,
    });
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    expect(screen.getByTestId('trust-summary')).toHaveTextContent('no open findings');
    expect(screen.queryByTestId(/trust-alert-/)).toBeNull();
    expect(screen.getByRole('tab', { name: 'Install details' })).toBeInTheDocument();
  });

  it('has a visible close button', async () => {
    const user = userEvent.setup();
    render(<MarketplacePage />);
    await user.click(screen.getByRole('button', { name: /^Review Helper/ }));
    await user.click(within(screen.getByRole('dialog', { name: 'Review Helper' })).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'Review Helper' })).toBeNull();
  });
});

describe('MarketplacePage updates to review', () => {
  it('sums updates across every administered org and opens when any has some', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', org_id: 'org_1' } });
    hooks.useMyOrgs.mockReturnValue({
      data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }, { org_id: 'org_2', name: 'Globex', role: 'owner' }],
      isLoading: false,
    });
    hooks.useHeldUpdateCounts.mockReturnValue({ total: 3, more: false, byOrg: { org_1: 0, org_2: 3 }, errors: {}, loaded: true });
    render(<MarketplacePage />);
    expect(hooks.useHeldUpdateCounts).toHaveBeenLastCalledWith(['org_1', 'org_2']);
    const details = screen.getByTestId('updates-to-review');
    expect(details).toHaveTextContent('Updates to review (3)across 2 organizations');
    expect(details).toHaveAttribute('open');
    // The queue starts on the org that has something waiting.
    expect(screen.getByTestId('held-queues')).toHaveAttribute('data-value', 'org_2');
  });

  it('leaves orgs below the Team plan out of the picker and still settles', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', org_id: 'org_1' } });
    hooks.useMyOrgs.mockReturnValue({
      data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }, { org_id: 'org_free', name: 'Free Co', role: 'owner' }],
      isLoading: false,
    });
    hooks.useHeldUpdateCounts.mockReturnValue({
      total: 1, more: false, byOrg: { org_1: 1, org_free: 0 },
      errors: { org_free: new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'Team plan required' } })) },
      loaded: true,
    });
    render(<MarketplacePage />);
    const details = screen.getByTestId('updates-to-review');
    expect(details).toHaveTextContent('Updates to review (1)');
    expect(details).not.toHaveTextContent('across');
    expect(screen.getByTestId('held-queues')).toHaveTextContent(/^org_1$/);
  });

  it('hides the section when every administered org is below the Team plan', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_free', name: 'Free Co', role: 'owner' }], isLoading: false });
    hooks.useHeldUpdateCounts.mockReturnValue({
      total: 0, more: false, byOrg: { org_free: 0 },
      errors: { org_free: new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'Team plan required' } })) },
      loaded: true,
    });
    render(<MarketplacePage />);
    expect(screen.queryByTestId('updates-to-review')).toBeNull();
    expect(screen.queryByText(/Review package updates/)).toBeNull();
  });

  it('does not load the queue while the section is closed, and loads it when opened', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', org_id: 'org_1' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }], isLoading: false });
    hooks.useHeldUpdateCounts.mockReturnValue({ total: 0, more: false, byOrg: { org_1: 0 }, errors: {}, loaded: true });
    render(<MarketplacePage />);
    expect(screen.queryByTestId('held-queues')).toBeNull();
    await user.click(screen.getByText(/Updates to review/));
    expect(screen.getByTestId('updates-to-review')).toHaveAttribute('open');
    expect(screen.getByTestId('held-queues')).toHaveAttribute('data-value', 'org_1');
  });

  it('waits for the counts before mounting the queue, so it never starts on a gated org', async () => {
    const user = userEvent.setup();
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', org_id: 'org_1' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }], isLoading: false });
    hooks.useHeldUpdateCounts.mockReturnValue({ total: 0, more: false, byOrg: {}, errors: {}, loaded: false });
    render(<MarketplacePage />);
    await user.click(screen.getByText(/Updates to review/));
    expect(screen.queryByTestId('held-queues')).toBeNull();
    expect(within(screen.getByTestId('updates-to-review')).getByText('Loading…')).toBeInTheDocument();
  });

  it('shows an unknown count when an org failed for a reason other than its plan', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', org_id: 'org_1' } });
    hooks.useMyOrgs.mockReturnValue({
      data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }, { org_id: 'org_2', name: 'Globex', role: 'admin' }],
      isLoading: false,
    });
    hooks.useHeldUpdateCounts.mockReturnValue({
      total: 0, more: false, byOrg: { org_1: 0, org_2: 0 }, errors: { org_2: new ApiError(500, 'boom') }, loaded: true,
    });
    const first = render(<MarketplacePage />);
    expect(screen.getByTestId('updates-to-review')).toHaveTextContent('Updates to review (?)');
    first.unmount();

    hooks.useHeldUpdateCounts.mockReturnValue({
      total: 2, more: false, byOrg: { org_1: 2, org_2: 0 }, errors: { org_2: new ApiError(500, 'boom') }, loaded: true,
    });
    render(<MarketplacePage />);
    expect(screen.getByTestId('updates-to-review')).toHaveTextContent('Updates to review (2+)');
  });

  it('marks a partial total and stays collapsed when nothing is waiting', () => {
    hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', org_id: 'org_1' } });
    hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'admin' }], isLoading: false });
    hooks.useHeldUpdateCounts.mockReturnValue({ total: 0, more: false, byOrg: { org_1: 0 }, errors: {}, loaded: true });
    const first = render(<MarketplacePage />);
    let details = screen.getByTestId('updates-to-review');
    expect(details).toHaveTextContent('Updates to review (0)');
    expect(details).not.toHaveAttribute('open');
    first.unmount();

    hooks.useHeldUpdateCounts.mockReturnValue({ total: 20, more: true, byOrg: { org_1: 20 }, errors: {}, loaded: true });
    render(<MarketplacePage />);
    details = screen.getByTestId('updates-to-review');
    expect(details).toHaveTextContent('Updates to review (20+)');
    expect(details).toHaveAttribute('open');
  });
});
