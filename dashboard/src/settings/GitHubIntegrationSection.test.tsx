import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GitHubIntegrationSection } from './GitHubIntegrationSection';

/**
 * GitHub App installation linking. Linking happens only through the
 * server's GitHub OAuth flow (startGitHubClaim -> github.com -> server
 * callback -> back here with ?github=connected|failed). A bare
 * ?installation_id in the URL must never trigger a claim.
 */

const mockAuth = vi.fn();
const mockGetGitHubInstallation = vi.fn();
const mockUpdateGitHubInstallation = vi.fn();
const mockStartGitHubClaim = vi.fn();
const mockGetGitHubClaimChoices = vi.fn();
const mockChooseGitHubInstallation = vi.fn();

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function renderSection() {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <GitHubIntegrationSection />
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

function setAuth() {
  mockAuth.mockReturnValue({
    auth: {
      apiKey: 'sk_test',
      baseUrl: 'http://test.api',
      client: {
        getGitHubInstallation: mockGetGitHubInstallation,
        updateGitHubInstallation: mockUpdateGitHubInstallation,
        startGitHubClaim: mockStartGitHubClaim,
        getGitHubClaimChoices: mockGetGitHubClaimChoices,
        chooseGitHubInstallation: mockChooseGitHubInstallation,
      },
    },
  });
}

function setLocation(search: string) {
  window.history.replaceState({}, '', `/settings${search}`);
}

const NOT_CONNECTED = {
  account_login: null,
  account_type: null,
  auto_comment: true,
  include_trust_score: true,
  include_session_links: true,
  ownership_verified: false,
  needs_reverification: false,
  claim_available: true,
  install_available: true,
};

// window.location.assign is not spy-able in jsdom; swap the whole object for
// the tests that navigate (same approach as LoginPage.test.tsx).
const realLocation = window.location;
const assign = vi.fn();
function mockNavigation() {
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...realLocation, href: realLocation.href, search: '', assign },
  });
}

describe('GitHubIntegrationSection', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockGetGitHubInstallation.mockReset();
    mockUpdateGitHubInstallation.mockReset();
    mockStartGitHubClaim.mockReset();
    mockGetGitHubClaimChoices.mockReset();
    mockChooseGitHubInstallation.mockReset();
    assign.mockReset();
    setAuth();
    setLocation('');
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
    setLocation('');
  });

  it('shows "Not connected" with Connect and Link buttons when OAuth is available', async () => {
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /connect github/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /link existing installation/i })).toBeInTheDocument();
  });

  it('Connect GitHub starts the install flow and sends the browser to GitHub', async () => {
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    mockStartGitHubClaim.mockResolvedValue(
      'https://github.com/apps/sessionfs/installations/new?state=abc',
    );
    mockNavigation();
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: /connect github/i }));
    await waitFor(() => {
      expect(mockStartGitHubClaim).toHaveBeenCalledWith('install');
    });
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith(
        'https://github.com/apps/sessionfs/installations/new?state=abc',
      );
    });
  });

  it('Link existing installation starts the authorize flow', async () => {
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    mockStartGitHubClaim.mockResolvedValue('https://github.com/login/oauth/authorize?state=x');
    mockNavigation();
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: /link existing installation/i }));
    await waitFor(() => {
      expect(mockStartGitHubClaim).toHaveBeenCalledWith('authorize');
    });
  });

  it('shows an error when the server refuses to start', async () => {
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    mockStartGitHubClaim.mockRejectedValue(new Error('github_claim_rate_limited'));
    mockNavigation();
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: /connect github/i }));
    expect(await screen.findByText(/couldn't start github sign-in/i)).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it('explains when linking is not configured on the server', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      ...NOT_CONNECTED,
      claim_available: false,
      install_available: false,
    });
    renderSection();
    expect(await screen.findByText(/not configured on this server/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect github/i })).not.toBeInTheDocument();
  });

  it('shows a verified connected state with the account and GitHub login', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: false,
      include_session_links: true,
      ownership_verified: true,
      needs_reverification: false,
      claim_verified_github_login: 'alice',
      claim_role: 'org_admin',
      repo_verification_active: true,
      claim_available: true,
      install_available: true,
    });
    renderSection();
    await waitFor(() => {
      expect(screen.getByText('acme-inc')).toBeInTheDocument();
    });
    expect(screen.getByText(/verified as @alice/i)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /auto-comment on prs/i })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /trust scores/i })).not.toBeChecked();
    expect(screen.queryByRole('button', { name: /verify ownership/i })).not.toBeInTheDocument();
  });

  it('flags a legacy claim and offers re-verification', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      installation_id: 777,
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
      ownership_verified: false,
      needs_reverification: true,
      claim_available: true,
      install_available: true,
    });
    mockStartGitHubClaim.mockResolvedValue('https://github.com/login/oauth/authorize?state=x');
    mockNavigation();
    renderSection();
    const button = await screen.findByRole('button', { name: /verify ownership with github/i });
    fireEvent.click(button);
    await waitFor(() => {
      expect(mockStartGitHubClaim).toHaveBeenCalledWith('authorize', 777);
    });
  });

  it('offers re-verification when an owner/admin proof has expired', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      installation_id: 888,
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
      ownership_verified: true,
      needs_reverification: false,
      claim_role: 'org_admin',
      repo_verification_active: false,
      proof_stale: true,
      claim_available: true,
      install_available: true,
    });
    mockStartGitHubClaim.mockResolvedValue('https://github.com/login/oauth/authorize?state=x');
    mockNavigation();
    renderSection();
    expect(await screen.findByText(/more than 7 days old/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /re-verify with github/i }));
    await waitFor(() => {
      expect(mockStartGitHubClaim).toHaveBeenCalledWith('authorize', 888);
    });
  });

  it('offers a targeted owner/admin re-check for a member-level claim', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      installation_id: 990,
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
      ownership_verified: true,
      needs_reverification: false,
      claim_role: 'member',
      repo_verification_active: false,
      proof_stale: false,
      claim_available: true,
      install_available: true,
    });
    mockStartGitHubClaim.mockResolvedValue('https://github.com/login/oauth/authorize?state=x');
    mockNavigation();
    renderSection();
    expect(await screen.findByText(/recheck your owner\/admin status/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /re-verify with github/i }));
    await waitFor(() => {
      expect(mockStartGitHubClaim).toHaveBeenCalledWith('authorize', 990);
    });
  });

  it('offers the same re-check for an installer-level claim', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      installation_id: 991,
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
      ownership_verified: true,
      needs_reverification: false,
      claim_role: 'installer',
      repo_verification_active: false,
      proof_stale: false,
      claim_available: true,
      install_available: true,
    });
    renderSection();
    expect(await screen.findByRole('button', { name: /re-verify with github/i })).toBeInTheDocument();
  });

  it('does not nag about re-verification for a fresh proof', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      installation_id: 889,
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
      ownership_verified: true,
      needs_reverification: false,
      claim_role: 'org_admin',
      repo_verification_active: true,
      proof_stale: false,
      claim_available: true,
      install_available: true,
    });
    renderSection();
    await screen.findByText('acme-inc');
    expect(screen.queryByRole('button', { name: /re-verify/i })).not.toBeInTheDocument();
  });

  it('on ?github=choose lists the installations and links exactly the picked one', async () => {
    setLocation('?github=choose');
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    mockGetGitHubClaimChoices.mockResolvedValue({
      choices: [
        { installation_id: 1, account_login: 'acme', account_type: 'Organization' },
        { installation_id: 2, account_login: 'beta', account_type: 'Organization' },
      ],
    });
    mockChooseGitHubInstallation.mockResolvedValue({ ...NOT_CONNECTED, account_login: 'beta' });
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: /beta/i }));
    await waitFor(() => {
      expect(mockChooseGitHubInstallation).toHaveBeenCalledTimes(1);
    });
    expect(mockChooseGitHubInstallation).toHaveBeenCalledWith(2);
    expect(await screen.findByText(/linked and ownership verified/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /acme/i })).not.toBeInTheDocument();
  });

  it('shows an expired chooser message when nothing is left to choose', async () => {
    setLocation('?github=choose');
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    mockGetGitHubClaimChoices.mockResolvedValue({ choices: [] });
    renderSection();
    expect(await screen.findByText(/this choice has expired/i)).toBeInTheDocument();
  });

  it('reports a failed choice with the generic message', async () => {
    setLocation('?github=choose');
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    mockGetGitHubClaimChoices.mockResolvedValue({
      choices: [{ installation_id: 1, account_login: 'acme', account_type: 'Organization' }],
    });
    mockChooseGitHubInstallation.mockRejectedValue(new Error('github_claim_failed'));
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: /acme/i }));
    expect(await screen.findByText(/couldn't link the github installation/i)).toBeInTheDocument();
  });

  it('never claims from a bare ?installation_id in the URL', async () => {
    setLocation('?installation_id=54321&setup_action=install');
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    renderSection();
    await waitFor(() => {
      expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    });
    expect(mockUpdateGitHubInstallation).not.toHaveBeenCalled();
    expect(mockStartGitHubClaim).not.toHaveBeenCalled();
  });

  it('reports ?github=connected once the server confirms a verified link', async () => {
    setLocation('?github=connected');
    mockGetGitHubInstallation.mockResolvedValue({
      ...NOT_CONNECTED,
      installation_id: 5,
      account_login: 'acme',
      account_type: 'Organization',
      ownership_verified: true,
      needs_reverification: false,
      repo_verification_active: true,
    });
    renderSection();
    expect(await screen.findByText(/linked and ownership verified/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(window.location.search).toBe('');
    });
  });

  it('does not trust a forged ?github=connected', async () => {
    setLocation('?github=connected');
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    renderSection();
    await screen.findByText(/not connected/i);
    expect(screen.queryByText(/linked and ownership verified/i)).not.toBeInTheDocument();
    // The parameter still triggers a refetch of the authenticated settings.
    expect(mockGetGitHubInstallation).toHaveBeenCalled();
  });

  it('does not claim success for an unverified (legacy) link', async () => {
    setLocation('?github=connected');
    mockGetGitHubInstallation.mockResolvedValue({
      ...NOT_CONNECTED,
      installation_id: 5,
      account_login: 'acme',
      account_type: 'Organization',
      ownership_verified: false,
      needs_reverification: true,
    });
    renderSection();
    await screen.findByText('acme');
    expect(screen.queryByText(/linked and ownership verified/i)).not.toBeInTheDocument();
  });

  it('reports ?github=failed with one generic message and strips the parameter', async () => {
    setLocation('?github=failed');
    mockGetGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    renderSection();
    expect(await screen.findByText(/couldn't link the github installation/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(window.location.search).toBe('');
    });
  });
});
