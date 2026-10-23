import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsPage from './SettingsPage';

/**
 * Full-page tests: the GitHub link flow returns to /settings?github=...,
 * and the outcome notices + installation chooser must actually be visible
 * (they live in the Integrations section, not the default Account tab).
 */

const mockAuth = vi.fn();
const client = {
  getMe: vi.fn(),
  getGitHubInstallation: vi.fn(),
  updateGitHubInstallation: vi.fn(),
  startGitHubClaim: vi.fn(),
  getGitHubClaimChoices: vi.fn(),
  chooseGitHubInstallation: vi.fn(),
  getGitLabSettings: vi.fn(),
};

vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

const NOT_CONNECTED = {
  installation_id: null,
  account_login: null,
  account_type: null,
  auto_comment: true,
  include_trust_score: true,
  include_session_links: true,
  claim_available: true,
  install_available: true,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/settings/:section" element={<SettingsPage />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

const realLocation = window.location;
const assign = vi.fn();

describe('SettingsPage GitHub return handling', () => {
  beforeEach(() => {
    Object.values(client).forEach((fn) => fn.mockReset());
    assign.mockReset();
    client.getMe.mockResolvedValue({ email: 'a@example.com', tier: 'pro' });
    client.getGitHubInstallation.mockResolvedValue(NOT_CONNECTED);
    client.getGitLabSettings.mockResolvedValue({ instance_url: 'https://gitlab.com', has_token: false });
    mockAuth.mockReturnValue({
      auth: { apiKey: 'sk_test_0123456789abcdef', baseUrl: 'http://test.api', client },
      logout: vi.fn(),
    });
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
    window.history.replaceState({}, '', '/settings');
  });

  it('lands on Integrations and shows the chooser on ?github=choose', async () => {
    window.history.replaceState({}, '', '/settings?github=choose');
    client.getGitHubClaimChoices.mockResolvedValue({
      choices: [
        { installation_id: 1, account_login: 'acme', account_type: 'Organization' },
        { installation_id: 2, account_login: 'beta', account_type: 'Organization' },
      ],
    });
    client.chooseGitHubInstallation.mockResolvedValue({ ...NOT_CONNECTED, account_login: 'acme' });
    renderPage();
    expect(await screen.findByText(/can link more than one installation/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /acme/i }));
    await waitFor(() => expect(client.chooseGitHubInstallation).toHaveBeenCalledWith(1));
  });

  it('shows the success notice on ?github=connected once the server confirms it', async () => {
    window.history.replaceState({}, '', '/settings?github=connected');
    client.getGitHubInstallation.mockResolvedValue({
        ...NOT_CONNECTED,
        installation_id: 5,
        account_login: 'acme',
        account_type: 'Organization',
        ownership_verified: true,
        needs_reverification: false,
        repo_verification_active: true,
      });
    renderPage();
    expect(await screen.findByText(/linked and ownership verified/i)).toBeInTheDocument();
  });

  it('a forged ?github=connected opens the tab but shows no success', async () => {
    window.history.replaceState({}, '', '/settings?github=connected');
    renderPage();
    expect(await screen.findByText(/GitHub Integration/)).toBeInTheDocument();
    await screen.findAllByText(/not connected/i);
    expect(screen.queryByText(/linked and ownership verified/i)).not.toBeInTheDocument();
  });

  it('shows the failure notice on ?github=failed', async () => {
    window.history.replaceState({}, '', '/settings?github=failed');
    renderPage();
    expect(await screen.findByText(/couldn't link the github installation/i)).toBeInTheDocument();
  });

  it('stays on Account without a github outcome', async () => {
    renderPage();
    await waitFor(() => expect(client.getMe).toHaveBeenCalled());
    expect(screen.queryByText(/GitHub Integration/)).not.toBeInTheDocument();
  });

  it('connected: no hard-coded install link; Add installation uses the state-bound flow', async () => {
    window.history.replaceState({}, '', '/settings?github=connected');
    client.getGitHubInstallation.mockResolvedValue({
      ...NOT_CONNECTED,
      installation_id: 4242,
      account_login: 'acme',
      account_type: 'Organization',
      ownership_verified: true,
      repo_verification_active: true,
    });
    client.startGitHubClaim.mockResolvedValue(
      'https://github.com/apps/sessionfs-test/installations/new?state=s',
    );
    const { container } = renderPage();
    const configure = await screen.findByRole('link', { name: /configure on github/i });
    expect(configure).toHaveAttribute(
      'href',
      'https://github.com/organizations/acme/settings/installations/4242',
    );
    expect(container.innerHTML).not.toContain('installations/new');
    expect(container.innerHTML).not.toContain('sessionfs-ai-context');
    // One linked installation per account: the UI says what Add does.
    expect(screen.getByText(/adding another replaces/i)).toHaveTextContent(
      /replaces acme: it is unlinked and its PR comments stop/i,
    );
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...realLocation, href: realLocation.href, search: '', assign },
    });
    fireEvent.click(screen.getByRole('button', { name: /add installation/i }));
    await waitFor(() => expect(client.startGitHubClaim).toHaveBeenCalledWith('install'));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith('https://github.com/apps/sessionfs-test/installations/new?state=s'),
    );
  });

  it('user-account installation links to the personal installation settings', async () => {
    window.history.replaceState({}, '', '/settings?github=connected');
    client.getGitHubInstallation.mockResolvedValue({
      ...NOT_CONNECTED,
      installation_id: 77,
      account_login: 'alice',
      account_type: 'User',
      ownership_verified: true,
      repo_verification_active: true,
    });
    renderPage();
    expect(await screen.findByRole('link', { name: /configure on github/i })).toHaveAttribute(
      'href',
      'https://github.com/settings/installations/77',
    );
  });

  it('hides Add installation when the install flow is not configured', async () => {
    window.history.replaceState({}, '', '/settings?github=connected');
    client.getGitHubInstallation.mockResolvedValue({
      ...NOT_CONNECTED,
      installation_id: 77,
      account_login: 'alice',
      account_type: 'User',
      ownership_verified: true,
      repo_verification_active: true,
      install_available: false,
    });
    renderPage();
    await screen.findByRole('link', { name: /configure on github/i });
    expect(screen.queryByRole('button', { name: /add installation/i })).not.toBeInTheDocument();
  });
});
