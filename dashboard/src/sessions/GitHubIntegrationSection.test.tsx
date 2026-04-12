import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GitHubIntegrationSection } from './SettingsPage';

/**
 * Tests for the GitHub App installation claim flow added in response to the
 * SCM pre-release review. Covers the useEffect that parses
 * ?installation_id=X&setup_action=install from the URL and calls the
 * authenticated claim endpoint, including the happy path, the error banner,
 * and the "no query params" no-op.
 */

const mockAuth = vi.fn();
const mockGetGitHubInstallation = vi.fn();
const mockUpdateGitHubInstallation = vi.fn();

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
      <GitHubIntegrationSection />
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
      },
    },
  });
}

function setLocation(search: string) {
  // jsdom lets us write to window.history / window.location.pathname
  window.history.replaceState({}, '', `/settings${search}`);
}

describe('GitHubIntegrationSection', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockGetGitHubInstallation.mockReset();
    mockUpdateGitHubInstallation.mockReset();
    setAuth();
    setLocation('');
  });

  afterEach(() => {
    setLocation('');
  });

  it('shows "Not connected" and a Connect button when the user has no installation', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: null,
      account_type: null,
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    });
    expect(screen.getByRole('link', { name: /connect github/i })).toBeInTheDocument();
  });

  it('shows a green connected state with the account name when already installed', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: 'acme-inc',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: false,
      include_session_links: true,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByText('acme-inc')).toBeInTheDocument();
    });
    expect(screen.getByText(/Organization/i)).toBeInTheDocument();
    // The toggle checkboxes reflect the settings. Labels in the source:
    // "Auto-comment on PRs", "Include trust scores", "Include session links".
    const autoComment = screen.getByRole('checkbox', { name: /auto-comment on prs/i });
    expect(autoComment).toBeChecked();
    const trustScore = screen.getByRole('checkbox', { name: /trust scores/i });
    expect(trustScore).not.toBeChecked();
  });

  it('does NOT auto-claim when no setup query params are present', async () => {
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: null,
      account_type: null,
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });
    setLocation('');

    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    });
    // Critical: updateGitHubInstallation must not have been called on mount
    expect(mockUpdateGitHubInstallation).not.toHaveBeenCalled();
  });

  it('auto-claims on mount when ?installation_id=X&setup_action=install is in the URL', async () => {
    setLocation('?installation_id=54321&setup_action=install');
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: null,
      account_type: null,
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });
    mockUpdateGitHubInstallation.mockResolvedValue({
      account_login: 'claimed-org',
      account_type: 'Organization',
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });

    renderSection();

    await waitFor(() => {
      expect(mockUpdateGitHubInstallation).toHaveBeenCalledWith({
        installation_id: 54321,
      });
    });

    // Query params should be stripped so browser refresh doesn't retry
    await waitFor(() => {
      expect(window.location.search).toBe('');
    });
  });

  it('shows a red error banner and still strips query params when the claim call fails', async () => {
    setLocation('?installation_id=77777&setup_action=install');
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: null,
      account_type: null,
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });
    mockUpdateGitHubInstallation.mockRejectedValue(
      new Error('Installation already claimed'),
    );

    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/couldn't link the github app installation/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/installation already claimed/i)).toBeInTheDocument();

    // Params stripped even on error
    await waitFor(() => {
      expect(window.location.search).toBe('');
    });
  });

  it('ignores a non-numeric installation_id', async () => {
    setLocation('?installation_id=not-a-number&setup_action=install');
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: null,
      account_type: null,
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    });
    expect(mockUpdateGitHubInstallation).not.toHaveBeenCalled();
  });

  it('ignores setup_action values other than "install"', async () => {
    setLocation('?installation_id=12345&setup_action=update');
    mockGetGitHubInstallation.mockResolvedValue({
      account_login: null,
      account_type: null,
      auto_comment: true,
      include_trust_score: true,
      include_session_links: true,
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    });
    expect(mockUpdateGitHubInstallation).not.toHaveBeenCalled();
  });
});
