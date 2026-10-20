import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PublishersPage from './PublishersPage';
import { workflowSnippet } from './publisherWorkflow';
import { ListingReviewBadge, ProvenancePanel, PublisherBadge } from './publisherParts';
import type { MarketplacePackage } from '../api/client';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useMe: vi.fn(),
    useMyOrgs: vi.fn(),
    useMyPublishers: vi.fn(),
    usePublisherListings: vi.fn(),
    claim: { mutateAsync: vi.fn(), isPending: false },
    start: { mutateAsync: vi.fn(), isPending: false },
    check: { mutateAsync: vi.fn(), isPending: false },
    revoke: { mutateAsync: vi.fn(), isPending: false },
    add: { mutateAsync: vi.fn(), isPending: false },
    remove: { mutateAsync: vi.fn(), isPending: false },
    addToast: vi.fn(),
  },
}));

vi.mock('../hooks/useMe', () => ({ useMe: hooks.useMe }));
vi.mock('../transfers/useTransfers', () => ({ useMyOrgs: hooks.useMyOrgs }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: hooks.addToast }) }));
vi.mock('../hooks/usePublishers', () => ({
  useMyPublishers: hooks.useMyPublishers,
  usePublisherListings: hooks.usePublisherListings,
  useClaimPublisher: () => hooks.claim,
  useStartVerification: () => hooks.start,
  useCheckVerification: () => hooks.check,
  useRevokeVerification: () => hooks.revoke,
  useAddTrustedPublisher: () => hooks.add,
  useRemoveTrustedPublisher: () => hooks.remove,
}));

const publisher = {
  id: 'pub_1',
  namespace: 'acme',
  display_name: 'Acme Inc.',
  owner_type: 'org',
  status: 'active',
  verified: false,
  badges: [],
  org_id: 'org_1',
  created_at: '2026-10-18T10:00:00Z',
  public_publishing: false,
  review_flags: [],
  verifications: [
    {
      id: 'spv_1', method: 'dns_txt', subject: 'acme.com', subject_display: 'acme.com', status: 'pending',
      status_reason: null, badge: null, verified_at: null, last_checked_at: null, last_success_at: null,
      consecutive_failures: 0, github_installation_id: null, reused_org_domain_verification: false,
      txt_record: 'sessionfs-verification=abc', txt_host: 'acme.com',
    },
  ],
  trusted_publishers: [
    {
      id: 'stp_1', provider: 'github', repository: 'acme/skills', repository_id: 1, repository_owner_id: 2,
      workflow: '.github/workflows/release.yml', environment: 'release', ref_pattern: 'refs/tags/v*',
      package_name: null, created_at: null, revoked_at: null,
    },
  ],
};

function renderPage() {
  return render(<MemoryRouter><PublishersPage /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useMe.mockReturnValue({ data: { user_id: 'user_1', tier: 'team' }, isLoading: false });
  hooks.useMyOrgs.mockReturnValue({ data: [{ org_id: 'org_1', name: 'Acme', role: 'owner' }], isLoading: false });
  hooks.useMyPublishers.mockReturnValue({ data: { items: [publisher], count: 1 }, isLoading: false, error: null });
  hooks.usePublisherListings.mockReturnValue({ data: { items: [], count: 0 } });
});

describe('PublishersPage', () => {
  it('claims a namespace for an org and validates the shape locally', async () => {
    const user = userEvent.setup();
    hooks.claim.mutateAsync.mockResolvedValue({ ...publisher, namespace: 'newco' });
    renderPage();
    const form = screen.getByRole('form', { name: 'Claim a namespace' });
    const claimButton = within(form).getByRole('button', { name: 'Claim' });
    await user.type(within(form).getByLabelText('Namespace'), 'ab');
    expect(claimButton).toBeDisabled();
    await user.clear(within(form).getByLabelText('Namespace'));
    await user.type(within(form).getByLabelText('Namespace'), 'NewCo');
    await user.click(claimButton);
    expect(hooks.claim.mutateAsync).toHaveBeenCalledWith({ namespace: 'newco' });
  });

  it('shows the TXT record for a pending DNS verification and re-checks it', async () => {
    const user = userEvent.setup();
    hooks.check.mutateAsync.mockResolvedValue({ ...publisher.verifications[0], status: 'verified' });
    renderPage();
    expect(screen.getByText('sessionfs-verification=abc')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Check now' }));
    expect(hooks.check.mutateAsync).toHaveBeenCalledWith({ publisherId: 'pub_1', verificationId: 'spv_1' });
    expect(hooks.addToast).toHaveBeenCalledWith('success', 'acme.com: verified');
  });

  it('starts a GitHub org verification with a numeric installation id', async () => {
    const user = userEvent.setup();
    hooks.start.mutateAsync.mockResolvedValue({ ...publisher.verifications[0], status: 'verified', subject_display: 'github.com/acme' });
    renderPage();
    const form = screen.getByRole('form', { name: 'Verify acme' });
    await user.click(within(form).getByRole('combobox', { name: 'Method' }));
    await user.click(screen.getByRole('option', { name: 'GitHub organization' }));
    await user.type(within(form).getByLabelText('GitHub App installation id'), '4242');
    await user.click(within(form).getByRole('button', { name: 'Verify' }));
    expect(hooks.start.mutateAsync).toHaveBeenCalledWith({
      publisherId: 'pub_1',
      body: { method: 'github_org', installation_id: 4242 },
    });
  });

  it('lists and adds trusted publishers', async () => {
    const user = userEvent.setup();
    hooks.add.mutateAsync.mockResolvedValue({});
    renderPage();
    expect(screen.getByText(/acme\/skills \(#1\)/)).toBeInTheDocument();
    const form = screen.getByRole('form', { name: 'Add trusted publisher for acme' });
    await user.type(within(form).getByLabelText('Repository (owner/repo)'), 'acme/tools');
    await user.click(within(form).getByRole('button', { name: 'Add trusted publisher' }));
    expect(hooks.add.mutateAsync).toHaveBeenCalledWith({
      publisherId: 'pub_1',
      body: { repository: 'acme/tools', workflow: 'release.yml', environment: 'release', ref_pattern: 'refs/tags/v*' },
    });
  });

  it('renders the empty state', () => {
    hooks.useMyPublishers.mockReturnValue({ data: { items: [], count: 0 }, isLoading: false, error: null });
    renderPage();
    expect(screen.getByText('No publishers yet')).toBeInTheDocument();
  });

  it('example workflow uses OIDC and no stored key', () => {
    const snippet = workflowSnippet('acme');
    expect(snippet).toContain('id-token: write');
    expect(snippet).toContain('persist-credentials: false');
    expect(snippet).toContain('--trusted-publishing --namespace acme');
    expect(snippet).not.toMatch(/SESSIONFS_API_KEY/);
  });
});

function pkg(extra: Partial<MarketplacePackage>): MarketplacePackage {
  return {
    listing_id: 'sml_1', collection: 'acme', visibility: 'public', verification_state: 'verified',
    package_id: 'spk_1', namespace: 'acme', name: 'review-helper', display_name: 'Review Helper',
    description: '', version_id: 'skv_1', version: '1.0.0', content_hash: 'sha256:x',
    source: { type: 'hosted-upload', locator: 'x', subdir: '', commit: null },
    risk: {}, compatibility: {}, license: {}, signature: {}, blob_size_bytes: 1,
    published_at: '2026-10-18T10:00:00Z', ...extra,
  } as MarketplacePackage;
}

describe('publisher parts', () => {
  it('shows the method-and-subject badge only for a verified, active publisher', () => {
    const verified = pkg({ publisher: { id: 'p', namespace: 'acme', display_name: 'Anthropic', owner_type: 'org', status: 'active', verified: true, badges: ['verified: acme.com'] } });
    const { rerender } = render(<PublisherBadge item={verified} />);
    expect(screen.getByText('verified: acme.com')).toBeInTheDocument();
    expect(screen.queryByText('Anthropic')).toBeNull();
    rerender(<PublisherBadge item={pkg({ publisher: { id: 'p', namespace: 'acme', display_name: 'Acme', owner_type: 'org', status: 'active', verified: false, badges: [] } })} />);
    expect(screen.getByText('unverified publisher')).toBeInTheDocument();
    rerender(<PublisherBadge item={pkg({ publisher: { id: 'p', namespace: 'acme', display_name: 'Acme', owner_type: 'org', status: 'suspended', verified: false, badges: [] } })} />);
    expect(screen.getByText('publisher suspended')).toBeInTheDocument();
    rerender(<ListingReviewBadge item={pkg({ moderation_state: 'pending_review' })} />);
    expect(screen.getByText('pending review')).toBeInTheDocument();
  });

  it('renders provenance links only from validated fields', () => {
    render(
      <ProvenancePanel
        item={pkg({
          signature: {
            kind: 'github_oidc', claim: 'publication', published_from_repo: 'github.com/acme/skills',
            published_from_repo_id: 1, publishing_run_sha: '0123456789abcdef0123456789abcdef01234567',
            publishing_workflow: '.github/workflows/release.yml', publishing_ref: 'refs/tags/v1.0.0',
            publishing_environment: 'release', publishing_event: 'push', publishing_run_id: '42',
            runner_environment: 'github-hosted', build_provenance: null,
          },
        })}
      />,
    );
    expect(screen.getByRole('link', { name: 'github.com/acme/skills' })).toHaveAttribute('href', 'https://github.com/acme/skills');
    expect(screen.getByRole('link', { name: '0123456789ab' })).toHaveAttribute(
      'href', 'https://github.com/acme/skills/commit/0123456789abcdef0123456789abcdef01234567',
    );
    expect(screen.getByRole('link', { name: '#42' })).toHaveAttribute('href', 'https://github.com/acme/skills/actions/runs/42');
    expect(screen.getByRole('heading', { name: 'Published from' })).toBeInTheDocument();
    expect(screen.getByText(/not how it was built/)).toBeInTheDocument();
    expect(screen.queryByText(/Build provenance/)).toBeNull();
  });

  it('refuses to link a hostile repository value', () => {
    render(
      <ProvenancePanel
        item={pkg({ signature: { kind: 'github_oidc', published_from_repo: 'javascript:alert(1)//github.com/a/b', publishing_run_sha: 'x', publishing_workflow: 'w', publishing_run_id: '1' } })}
      />,
    );
    expect(screen.queryAllByRole('link')).toHaveLength(0);
  });

  it('says when a version has no CI provenance', () => {
    render(<ProvenancePanel item={pkg({})} />);
    expect(screen.getByText(/No CI publishing run/)).toBeInTheDocument();
  });
});
