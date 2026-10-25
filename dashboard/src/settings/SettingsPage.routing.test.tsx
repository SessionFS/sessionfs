import { fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsPage from './SettingsPage';

/**
 * Section routing for the settings shell: /settings/:section addressing,
 * the default section, and that the static billing/organization routes are
 * not swallowed by the dynamic one.
 */

const mockAuth = vi.fn();
const client = {
  getMe: vi.fn(),
  getGitHubInstallation: vi.fn(),
  getAuditTrigger: vi.fn(),
  getSyncSettings: vi.fn(),
  getJudgeSettings: vi.fn(),
};

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

// Preferences now renders a notification email preference; stub its hooks and
// the toast provider so the section stays renderable without an AuthProvider
// tree or the real query client wiring.
vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ addToast: vi.fn() }),
}));
vi.mock('../hooks/useTicketNotifications', () => ({
  useNotificationSettings: () => ({ data: { ticket_email: 'your_turn' } }),
  useUpdateNotificationSettings: () => ({ mutate: vi.fn() }),
}));

// Heavy sections are covered by their own tests; stub the ones that fetch.
vi.mock('./DataProtectionSection', () => ({ default: () => <div>data-protection-stub</div> }));
vi.mock('./IntegrationsSection', () => ({ default: () => <div>integrations-stub</div> }));
vi.mock('./SyncSection', () => ({ default: () => <div>sync-stub</div> }));

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname + loc.search}</div>;
}

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <LocationProbe />
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/settings/:section" element={<SettingsPage />} />
          <Route path="/settings/billing" element={<div>billing-page</div>} />
          <Route path="/settings/organization" element={<div>organization-page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('SettingsPage section routing', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    client.getMe.mockReset();
    client.getMe.mockResolvedValue({ email: 'a@example.com', tier: 'pro', email_verified: true });
    mockAuth.mockReturnValue({
      auth: { apiKey: 'sk_test_0123456789abcdef', baseUrl: 'http://test.api', client },
      logout: vi.fn(),
    });
  });

  it('bare /settings redirects to the default Account section', async () => {
    renderAt('/settings');
    expect(await screen.findByText('a@example.com')).toBeInTheDocument();
    expect(screen.getByTestId('loc')).toHaveTextContent('/settings/account');
  });

  it('renders each section at /settings/:section', async () => {
    renderAt('/settings/data-protection');
    expect(await screen.findByText('data-protection-stub')).toBeInTheDocument();
    expect(screen.queryByText('a@example.com')).not.toBeInTheDocument();
  });

  it('shows the Preferences appearance control at /settings/preferences', async () => {
    renderAt('/settings/preferences');
    expect(await screen.findByText('Appearance')).toBeInTheDocument();
  });

  it('an unknown section falls back to Account', async () => {
    renderAt('/settings/nope');
    expect(await screen.findByText('a@example.com')).toBeInTheDocument();
    expect(screen.getByTestId('loc')).toHaveTextContent('/settings/account');
  });

  it('does not swallow /settings/billing or /settings/organization', () => {
    const first = renderAt('/settings/billing');
    expect(screen.getByText('billing-page')).toBeInTheDocument();
    first.unmount();
    renderAt('/settings/organization');
    expect(screen.getByText('organization-page')).toBeInTheDocument();
  });

  it('sub-nav links navigate between sections', async () => {
    renderAt('/settings/account');
    await screen.findByText('a@example.com');
    const nav = screen.getByRole('navigation', { name: /settings sections/i });
    fireEvent.click(within(nav).getByRole('link', { name: 'Sync' }));
    expect(await screen.findByText('sync-stub')).toBeInTheDocument();
    expect(screen.getByTestId('loc')).toHaveTextContent('/settings/sync');
  });

  it('lists the five user-facing sections', async () => {
    renderAt('/settings/account');
    await screen.findByText('a@example.com');
    const nav = screen.getByRole('navigation', { name: /settings sections/i });
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Account',
      'Preferences',
      'Integrations',
      'Data protection',
      'Sync',
    ]);
  });

  it('GitHub return on bare /settings lands on Integrations and keeps other params', async () => {
    renderAt('/settings?github=failed&keep=1');
    expect(await screen.findByText('integrations-stub')).toBeInTheDocument();
    expect(screen.getByTestId('loc')).toHaveTextContent('/settings/integrations?github=failed&keep=1');
  });

  it('GitHub return on another section is redirected to Integrations', async () => {
    renderAt('/settings/sync?github=choose');
    expect(await screen.findByText('integrations-stub')).toBeInTheDocument();
    expect(screen.getByTestId('loc')).toHaveTextContent('/settings/integrations?github=choose');
  });
});
