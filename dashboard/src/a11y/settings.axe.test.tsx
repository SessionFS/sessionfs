import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsPage from '../settings/SettingsPage';
import { axe } from '../test/axe';

const client = {
  getMe: vi.fn(),
  getGitHubInstallation: vi.fn(),
  getAuditTrigger: vi.fn(),
  getSyncSettings: vi.fn(),
  getJudgeSettings: vi.fn(),
};

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    auth: { apiKey: 'sk_test_fake', baseUrl: 'http://test.api', client },
    logout: vi.fn(),
  }),
}));

// Fetch-heavy sections have their own suites; the shell, Account and
// Preferences sections are audited for real.
vi.mock('../settings/DataProtectionSection', () => ({ default: () => <p>data protection</p> }));
vi.mock('../settings/IntegrationsSection', () => ({ default: () => <p>integrations</p> }));
vi.mock('../settings/SyncSection', () => ({ default: () => <p>sync</p> }));

beforeEach(() => {
  client.getMe.mockResolvedValue({ email: 'a@example.test', tier: 'pro', email_verified: true });
});

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <main>
          <Routes>
            <Route path="/settings/:section" element={<SettingsPage />} />
          </Routes>
        </main>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Settings shell', () => {
  it('Account section has no axe violations', async () => {
    const { container } = renderAt('/settings/account');
    await screen.findByText('a@example.test');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('Preferences section has no axe violations', async () => {
    const { container } = renderAt('/settings/preferences');
    await screen.findByText('Appearance');
    expect(await axe(container)).toHaveNoViolations();
  });
});
