import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import OrgPage from '../org/OrgPage';
import { axe } from '../test/axe';

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { apiKey: 'sk_test_fake', baseUrl: 'http://test.api' } }),
}));
// Tab bodies with their own suites and data hooks.
vi.mock('../org/OrgSettingsTab', () => ({ default: () => <p>org settings</p> }));
vi.mock('../org/SsoTab', () => ({ default: () => <p>sso config</p> }));
vi.mock('../org/OrgPersonasSection', () => ({ default: () => <p>org personas</p> }));

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as unknown as Response;

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/api/v1/org')) {
      return ok({
        org: { id: 'org_x', name: 'Acme', slug: 'acme', tier: 'enterprise', seats_used: 2, seats_limit: 100 },
        members: [
          { user_id: 'u_1', email: 'admin@example.test', display_name: 'Admin One', role: 'admin', joined_at: '2026-03-01T12:00:00Z' },
          { user_id: 'u_2', email: 'member@example.test', display_name: null, role: 'member', joined_at: '2026-03-02T12:00:00Z' },
        ],
        current_user_role: 'admin',
      });
    }
    if (url.endsWith('/api/v1/org/invites')) return ok({ invites: [] });
    return ok({});
  }));
});
afterEach(() => vi.unstubAllGlobals());

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings/organization']}>
        <main>
          <OrgPage />
        </main>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Organization page', () => {
  it('Members tab has no axe violations', async () => {
    const { container } = renderPage();
    await screen.findByText('member@example.test');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('Security tab has no axe violations', async () => {
    const { container } = renderPage();
    await screen.findByText('member@example.test');
    await userEvent.setup().click(screen.getByRole('tab', { name: 'Security' }));
    await screen.findByText('sso config');
    expect(await axe(container)).toHaveNoViolations();
  });
});
