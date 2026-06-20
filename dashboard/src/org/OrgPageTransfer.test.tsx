import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import OrgPage from './OrgPage';

/**
 * UI coverage for v0.11.0 ownership transfer on the Org page:
 *   - owner sees a "Transfer ownership" action and can initiate to an admin
 *   - the target sees an Accept/Decline banner (POST .../accept | .../cancel)
 *   - the initiator sees a pending banner with Cancel (POST .../cancel)
 *
 * Backend (live in prod):
 *   POST   /api/v1/orgs/{id}/owner/transfer            { to_user_id }
 *   POST   /api/v1/orgs/{id}/owner/transfer/{tid}/accept
 *   POST   /api/v1/orgs/{id}/owner/transfer/{tid}/cancel
 *   GET    /api/v1/orgs/{id}/owner/transfer            → pending transfer | {}
 */

const { mockAuth, mockMe } = vi.hoisted(() => ({ mockAuth: vi.fn(), mockMe: vi.fn() }));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../hooks/useMe', () => ({ useMe: () => mockMe() }));
vi.mock('./OrgSettingsTab', () => ({
  default: ({ orgId }: { orgId: string }) => <div data-testid={`org-settings-stub-${orgId}`} />,
}));
vi.mock('./ActivateLicensePanel', () => ({ default: () => <div data-testid="activate-panel" /> }));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}
function renderPage() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <OrgPage />
    </QueryClientProvider>,
  );
}

type FetchHandler = (url: string, init?: RequestInit) => Response | Promise<Response>;
function stubFetch(handler: FetchHandler) {
  const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal('fetch', mock);
  return mock;
}
function jsonOk<T>(body: T): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

const OWNER = { user_id: 'u_owner', email: 'owner@example.com', display_name: null, role: 'owner', joined_at: '2026-01-01T00:00:00Z' };
const ADMIN = { user_id: 'u_admin', email: 'admin@example.com', display_name: null, role: 'admin', joined_at: '2026-02-01T00:00:00Z' };
const MEMBER = { user_id: 'u_member', email: 'member@example.com', display_name: null, role: 'member', joined_at: '2026-03-01T00:00:00Z' };

function orgInfo(currentRole: string) {
  return {
    org: { id: 'org_x', name: 'Acme', slug: 'acme', tier: 'enterprise', seats_used: 3, seats_limit: 50 },
    members: [OWNER, ADMIN, MEMBER],
    current_user_role: currentRole,
  };
}

describe('OrgPage — ownership transfer', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockMe.mockReset();
    mockAuth.mockReturnValue({ auth: { apiKey: 'sk_test', baseUrl: 'http://test.api' } });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('owner can initiate a transfer to an admin', async () => {
    mockMe.mockReturnValue({ data: { user_id: 'u_owner' } });
    let transferPosted: RequestInit | undefined;
    stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org')) return jsonOk(orgInfo('owner'));
      if (url.endsWith('/api/v1/org/invites')) return jsonOk({ invites: [] });
      if (url.endsWith('/owner/transfer') && (!init || init.method === undefined)) return jsonOk({});
      if (url.endsWith('/owner/transfer') && init?.method === 'POST') {
        transferPosted = init;
        return jsonOk({ transfer_id: 7, org_id: 'org_x', from_user_id: 'u_owner', to_user_id: 'u_admin', status: 'pending', created_at: '2026-06-20T00:00:00Z', expires_at: null });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();
    const user = userEvent.setup();

    const transferBtn = await screen.findByRole('button', { name: /transfer ownership/i });
    await user.click(transferBtn);

    // Dialog open — pick the admin from the custom Select listbox.
    await user.click(await screen.findByRole('combobox', { name: /select the new owner/i }));
    await user.click(await screen.findByRole('option', { name: 'admin@example.com' }));

    // Confirm — the dialog's submit button.
    const dialogSubmit = screen
      .getAllByRole('button', { name: /transfer ownership/i })
      .at(-1)!;
    await user.click(dialogSubmit);

    await waitFor(() => expect(transferPosted).toBeDefined());
    expect(JSON.parse(String(transferPosted!.body))).toEqual({ to_user_id: 'u_admin' });
  });

  it('target sees an Accept/Decline banner and Accept hits the accept endpoint', async () => {
    mockMe.mockReturnValue({ data: { user_id: 'u_admin' } });
    let acceptCalled = '';
    stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org')) return jsonOk(orgInfo('admin'));
      if (url.endsWith('/api/v1/org/invites')) return jsonOk({ invites: [] });
      if (url.endsWith('/owner/transfer') && (!init || init.method === undefined)) {
        return jsonOk({ transfer_id: 7, org_id: 'org_x', from_user_id: 'u_owner', to_user_id: 'u_admin', status: 'pending', created_at: '2026-06-20T00:00:00Z', expires_at: null });
      }
      if (url.includes('/owner/transfer/7/accept') && init?.method === 'POST') {
        acceptCalled = url;
        return jsonOk({ status: 'accepted' });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText(/you've been offered ownership/i)).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /accept ownership/i }));

    await waitFor(() => expect(acceptCalled).toContain('/owner/transfer/7/accept'));
  });

  it('initiator sees a pending banner with Cancel that hits the cancel endpoint', async () => {
    mockMe.mockReturnValue({ data: { user_id: 'u_owner' } });
    let cancelCalled = '';
    stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org')) return jsonOk(orgInfo('owner'));
      if (url.endsWith('/api/v1/org/invites')) return jsonOk({ invites: [] });
      if (url.endsWith('/owner/transfer') && (!init || init.method === undefined)) {
        return jsonOk({ transfer_id: 7, org_id: 'org_x', from_user_id: 'u_owner', to_user_id: 'u_admin', status: 'pending', created_at: '2026-06-20T00:00:00Z', expires_at: null });
      }
      if (url.includes('/owner/transfer/7/cancel') && init?.method === 'POST') {
        cancelCalled = url;
        return jsonOk({ status: 'cancelled' });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText(/ownership transfer pending/i)).toBeInTheDocument());
    expect(screen.getByText(/waiting for/i)).toHaveTextContent('admin@example.com');
    await user.click(screen.getByRole('button', { name: /cancel transfer/i }));

    await waitFor(() => expect(cancelCalled).toContain('/owner/transfer/7/cancel'));
  });

  it('owner row is immutable — no Make Member / Remove on the owner', async () => {
    mockMe.mockReturnValue({ data: { user_id: 'u_owner' } });
    stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org')) return jsonOk(orgInfo('owner'));
      if (url.endsWith('/api/v1/org/invites')) return jsonOk({ invites: [] });
      if (url.endsWith('/owner/transfer') && (!init || init.method === undefined)) return jsonOk({});
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();
    await waitFor(() => expect(screen.getByRole('heading', { name: /acme/i })).toBeInTheDocument());

    // The member row has a Remove button; the owner row does not → exactly the
    // non-owner rows (admin + member) expose Remove.
    expect(screen.getAllByRole('button', { name: /^remove$/i })).toHaveLength(2);
  });
});
