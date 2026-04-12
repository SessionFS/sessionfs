import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import BillingPage from './BillingPage';

const mockAuth = vi.fn();

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

/**
 * Helper: build a QueryClient per test so no shared cache pollution and
 * zero-retry so failed requests surface immediately in tests.
 */
function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function renderPage() {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <BillingPage />
    </QueryClientProvider>,
  );
}

type BillingStatus = {
  tier: string;
  storage_used_bytes: number;
  storage_limit_bytes: number;
  stripe_customer_id: string | null;
  has_subscription: boolean;
  has_personal_subscription: boolean;
  is_org_member: boolean;
  org_role: string | null;
  is_beta: boolean;
};

function stubFetch(status: BillingStatus | Error) {
  const mock = vi.fn().mockImplementation(async (url: string) => {
    if (typeof url === 'string' && url.includes('/api/v1/billing/status')) {
      if (status instanceof Error) throw status;
      return {
        ok: true,
        status: 200,
        json: async () => status,
      } as unknown as Response;
    }
    // Other endpoints (checkout/portal) — tests override per-case
    return {
      ok: true,
      status: 200,
      json: async () => ({ checkout_url: 'https://checkout.stripe.example/abc' }),
    } as unknown as Response;
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

describe('BillingPage', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockAuth.mockReturnValue({
      auth: {
        apiKey: 'sk_test_key',
        baseUrl: 'http://test.api',
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows a loading indicator before billing status resolves', () => {
    const pending = new Promise(() => {});
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValue(pending),
    );
    renderPage();
    expect(screen.getByText(/loading billing/i)).toBeInTheDocument();
  });

  it('free tier without a subscription shows upgrade buttons', async () => {
    stubFetch({
      tier: 'free',
      storage_used_bytes: 0,
      storage_limit_bytes: 0,
      stripe_customer_id: null,
      has_subscription: false,
      has_personal_subscription: false,
      is_org_member: false,
      org_role: null,
      is_beta: false,
    });

    renderPage();

    // "Free" appears in both the hero "Current plan" panel AND the tier card.
    // The hero uses capitalize on `currentTier`; the tier card header is
    // explicitly "Free" — match "Current plan" marker instead.
    await waitFor(() => {
      expect(screen.getAllByText(/free/i).length).toBeGreaterThan(0);
    });

    // No "Manage Subscription" button — no active subscription
    expect(screen.queryByRole('button', { name: /manage subscription/i })).not.toBeInTheDocument();

    // Upgrade buttons are present on paid tiers
    const upgradeButtons = screen.getAllByRole('button', { name: /upgrade/i });
    expect(upgradeButtons.length).toBeGreaterThanOrEqual(1);
  });

  it('pro personal subscription shows manage + invoices buttons', async () => {
    stubFetch({
      tier: 'pro',
      storage_used_bytes: 100_000_000,
      storage_limit_bytes: 500_000_000,
      stripe_customer_id: 'cus_personal',
      has_subscription: true,
      has_personal_subscription: true,
      is_org_member: false,
      org_role: null,
      is_beta: false,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /manage subscription/i })).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /view invoices/i })).toBeInTheDocument();
    // The "Pro" tier card should have a Current-plan marker
    expect(screen.getAllByText(/current plan/i).length).toBeGreaterThan(0);
  });

  it('team org admin can manage the subscription', async () => {
    stubFetch({
      tier: 'team',
      storage_used_bytes: 5_000_000_000,
      storage_limit_bytes: 10_000_000_000,
      stripe_customer_id: 'cus_org',
      has_subscription: true,
      has_personal_subscription: false,
      is_org_member: true,
      org_role: 'admin',
      is_beta: false,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /manage subscription/i })).toBeInTheDocument();
    });
    // Admin shouldn't see the "managed by admin" notice
    expect(screen.queryByText(/managed by your organization admin/i)).not.toBeInTheDocument();
    // Nor the personal-sub warning
    expect(screen.queryByText(/personal subscription that is still active/i)).not.toBeInTheDocument();
  });

  it('team org non-admin sees the managed-by-admin notice', async () => {
    stubFetch({
      tier: 'team',
      storage_used_bytes: 0,
      storage_limit_bytes: 1_000_000_000,
      stripe_customer_id: 'cus_org',
      has_subscription: true,
      has_personal_subscription: false,
      is_org_member: true,
      org_role: 'member',
      is_beta: false,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/managed by your organization admin/i)).toBeInTheDocument();
    });
    // Non-admin should NOT see the manage-subscription button
    expect(screen.queryByRole('button', { name: /^manage subscription/i })).not.toBeInTheDocument();
  });

  it('org member with a leftover personal subscription sees the warning banner', async () => {
    stubFetch({
      tier: 'team',
      storage_used_bytes: 0,
      storage_limit_bytes: 1_000_000_000,
      stripe_customer_id: 'cus_personal',
      has_subscription: true,
      has_personal_subscription: true,
      is_org_member: true,
      org_role: 'member',
      is_beta: false,
    });

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText(/personal subscription that is still active/i),
      ).toBeInTheDocument();
    });
    // Clicking the banner's button calls portal with scope='personal'
    const manageBtn = screen.getByRole('button', { name: /manage personal subscription/i });
    expect(manageBtn).toBeInTheDocument();
  });

  it('clicking Manage Personal Subscription POSTs to the portal with scope=personal', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/api/v1/billing/status')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            tier: 'team',
            storage_used_bytes: 0,
            storage_limit_bytes: 1_000_000_000,
            stripe_customer_id: 'cus_personal',
            has_subscription: true,
            has_personal_subscription: true,
            is_org_member: true,
            org_role: 'member',
            is_beta: false,
          }),
        } as unknown as Response;
      }
      if (url.includes('/api/v1/billing/portal')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ portal_url: 'https://portal.stripe.example/x' }),
        } as unknown as Response;
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    // Stub navigation so window.location.href doesn't blow up jsdom
    const originalLocation = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...originalLocation, href: '' },
    });

    renderPage();
    const user = userEvent.setup();

    const btn = await screen.findByRole('button', { name: /manage personal subscription/i });
    await user.click(btn);

    await waitFor(() => {
      const portalCall = fetchMock.mock.calls.find(
        ([u]) => typeof u === 'string' && u.includes('/api/v1/billing/portal'),
      );
      expect(portalCall).toBeDefined();
      const body = JSON.parse(portalCall![1].body);
      expect(body.scope).toBe('personal');
    });

    Object.defineProperty(window, 'location', {
      configurable: true,
      value: originalLocation,
    });
  });

  it('beta mode (server says is_beta=true) hides checkout buttons', async () => {
    stubFetch({
      tier: 'free',
      storage_used_bytes: 0,
      storage_limit_bytes: 0,
      stripe_customer_id: null,
      has_subscription: false,
      has_personal_subscription: false,
      is_org_member: false,
      org_role: null,
      is_beta: true,
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getAllByText(/beta — all features included/i).length).toBeGreaterThan(0);
    });
    // Upgrade buttons replaced with "Coming soon"
    expect(screen.queryByRole('button', { name: /^upgrade$/i })).not.toBeInTheDocument();
    expect(screen.getAllByText(/coming soon/i).length).toBeGreaterThan(0);
  });

  it('billing status fetch error falls back to beta mode', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/api/v1/billing/status')) {
        return {
          ok: false,
          status: 500,
          json: async () => ({}),
        } as unknown as Response;
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    // isError → isBeta fallback → "Beta" banner appears
    await waitFor(() => {
      expect(screen.getAllByText(/beta — all features included/i).length).toBeGreaterThan(0);
    });
  });

  it('renders the 4-tier pricing grid with every tier name', async () => {
    stubFetch({
      tier: 'free',
      storage_used_bytes: 0,
      storage_limit_bytes: 0,
      stripe_customer_id: null,
      has_subscription: false,
      has_personal_subscription: false,
      is_org_member: false,
      org_role: null,
      is_beta: false,
    });

    renderPage();

    await waitFor(() => {
      // Tier headers use <h3>
      const headings = screen.getAllByRole('heading', { level: 3 });
      const names = headings.map((h) => h.textContent?.trim());
      for (const t of ['Free', 'Starter', 'Pro', 'Team']) {
        expect(names).toContain(t);
      }
    });
  });
});
