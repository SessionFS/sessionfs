import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import OrgPage from './OrgPage';

/**
 * UI coverage for the Organization management page. OrgPage uses raw
 * `fetch` (not a typed client) against:
 *   - GET  /api/v1/org          → org info + members
 *   - GET  /api/v1/org/invites  → pending invites (admin-only)
 *   - POST /api/v1/org/invite   → invite member
 *   - DELETE /api/v1/org/members/{id}
 *   - PUT  /api/v1/org/members/{id}/role
 *
 * Tests cover: loading, no-org state, admin view with member list and
 * invite form, non-admin view (no invite button), invite POST payload,
 * and role change POST payload.
 */

const { mockAuth } = vi.hoisted(() => ({
  mockAuth: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

// v0.10.0 Phase 6: OrgSettingsTab mounted on OrgPage hits a new endpoint
// (/api/v1/orgs/{id}/settings). Stub it out here so this suite stays
// focused on the existing OrgPage surface — OrgSettingsTab has its own
// dedicated test file.
vi.mock('./OrgSettingsTab', () => ({
  default: ({ orgId }: { orgId: string }) => (
    <div data-testid={`org-settings-stub-${orgId}`}>org settings</div>
  ),
}));

// v0.13.x: SsoTab is mounted on OrgPage for admins/owners. Stub it out
// so this suite stays focused on the existing OrgPage surface.
vi.mock('./SsoTab', () => ({
  default: ({ orgId }: { orgId: string }) => (
    <div data-testid={`sso-stub-${orgId}`}>sso config</div>
  ),
}));

// GH #70: OrgPersonasSection is mounted for admins/owners; it has its own
// test file.
vi.mock('./OrgPersonasSection', () => ({
  default: ({ orgId }: { orgId: string }) => (
    <div data-testid={`org-personas-stub-${orgId}`}>org personas</div>
  ),
}));

// GH #92: SecondaryOrgPage deep-links from a represented-persona badge; it
// has its own test file.
vi.mock('./SecondaryOrgPage', () => ({
  default: ({ orgId }: { orgId: string }) => (
    <div data-testid={`secondary-org-stub-${orgId}`}>secondary org</div>
  ),
}));

// The Integrations tab mounts the typed client; IntegrationsTab, JiraCard and
// ConfluenceCard have their own test files. Stub it so this suite can assert
// the tab's admin-only gating.
vi.mock('./integrations/IntegrationsTab', () => ({
  default: ({ orgId, tier }: { orgId: string; tier: string }) => (
    <div data-testid={`integrations-stub-${orgId}`}>integrations ({tier})</div>
  ),
}));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function renderPage(initialUrl = '/settings/organization') {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <OrgPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

type FetchHandler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function stubFetch(handler: FetchHandler) {
  const mock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const resp = await handler(url, init);
    return resp;
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function jsonOk<T>(body: T): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response;
}

function jsonError(status: number, body: Record<string, unknown> = {}): Response {
  return {
    ok: false,
    status,
    json: async () => body,
  } as unknown as Response;
}

describe('OrgPage', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockAuth.mockReturnValue({
      auth: { apiKey: 'sk_test', baseUrl: 'http://test.api' },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows a loading state before org info resolves', () => {
    const pending = new Promise(() => {});
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending));
    renderPage();
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('renders the "No Organization" empty state when user is not in an org', async () => {
    stubFetch((url) => {
      if (url.includes('/api/v1/org')) {
        return jsonOk({ org: null, members: [], current_user_role: null });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/no organization/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/organizations are available on team tier/i)).toBeInTheDocument();
    // CLI hint visible
    expect(screen.getByText(/sfs org create/i)).toBeInTheDocument();
  });

  it('admin view shows org header, member list, invite button', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/v1/org')) {
        return jsonOk({
          org: {
            id: 'org_x',
            name: 'SessionFS',
            slug: 'sessionfs',
            tier: 'enterprise',
            seats_used: 3,
            seats_limit: 100,
            storage_limit_bytes: 0,
            storage_used_bytes: 0,
          },
          members: [
            {
              user_id: 'u_1',
              email: 'admin@example.com',
              display_name: 'Admin One',
              role: 'admin',
              joined_at: '2026-03-01T12:00:00Z',
            },
            {
              user_id: 'u_2',
              email: 'member@example.com',
              display_name: null,
              role: 'member',
              joined_at: '2026-03-02T12:00:00Z',
            },
          ],
          current_user_role: 'admin',
        });
      }
      if (url.endsWith('/api/v1/org/invites')) {
        return jsonOk({ invites: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /sessionfs/i })).toBeInTheDocument();
    });
    expect(screen.getByText(/sessionfs . enterprise tier . 3\/100 seats/i)).toBeInTheDocument();
    expect(screen.getByText('admin@example.com')).toBeInTheDocument();
    expect(screen.getByText('member@example.com')).toBeInTheDocument();
    // Admin sees the invite button
    expect(screen.getByRole('button', { name: /invite member/i })).toBeInTheDocument();
    // GH #70 — admins get the org personas section (its own tab).
    await userEvent.setup().click(screen.getByRole('tab', { name: 'Personas' }));
    expect(screen.getByTestId('org-personas-stub-org_x')).toBeInTheDocument();
  });

  it('non-admin view hides the invite button', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/v1/org')) {
        return jsonOk({
          org: {
            id: 'org_x',
            name: 'SessionFS',
            slug: 'sessionfs',
            tier: 'enterprise',
            seats_used: 1,
            seats_limit: 100,
          },
          members: [
            {
              user_id: 'u_1',
              email: 'me@example.com',
              display_name: null,
              role: 'member',
              joined_at: '2026-03-01T12:00:00Z',
            },
          ],
          current_user_role: 'member',
        });
      }
      // /invites is not called for non-admins (enabled: false)
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /sessionfs/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /invite member/i })).not.toBeInTheDocument();
    expect(screen.queryByTestId('org-personas-stub-org_x')).not.toBeInTheDocument();
    // Admin-only Security and Integrations tabs are not offered; the policies
    // tab stays visible.
    expect(screen.queryByRole('tab', { name: 'Security' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Integrations' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Policies' })).toBeInTheDocument();
  });

  it('admins see the Integrations tab and its integrations', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/v1/org')) {
        return jsonOk({
          org: {
            id: 'org_x',
            name: 'SessionFS',
            slug: 'sessionfs',
            tier: 'enterprise',
            seats_used: 1,
            seats_limit: 100,
          },
          members: [
            {
              user_id: 'u_1',
              email: 'admin@example.com',
              display_name: 'Admin One',
              role: 'admin',
              joined_at: '2026-03-01T12:00:00Z',
            },
          ],
          current_user_role: 'admin',
        });
      }
      if (url.endsWith('/api/v1/org/invites')) {
        return jsonOk({ invites: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /sessionfs/i })).toBeInTheDocument();
    });
    await userEvent.setup().click(screen.getByRole('tab', { name: 'Integrations' }));
    expect(screen.getByTestId('integrations-stub-org_x')).toHaveTextContent('integrations (enterprise)');
  });

  it('clicking Invite Member opens the invite form and submits the POST', async () => {
    let invitePosted: RequestInit | undefined;
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org')) {
        return jsonOk({
          org: { id: 'org_x', name: 'SessionFS', slug: 'sessionfs', tier: 'enterprise', seats_used: 1, seats_limit: 100 },
          members: [
            {
              user_id: 'u_1',
              email: 'admin@example.com',
              display_name: null,
              role: 'admin',
              joined_at: '2026-03-01T12:00:00Z',
            },
          ],
          current_user_role: 'admin',
        });
      }
      if (url.endsWith('/api/v1/org/invites')) {
        return jsonOk({ invites: [] });
      }
      if (url.endsWith('/api/v1/org/invite') && init?.method === 'POST') {
        invitePosted = init;
        return jsonOk({ invite_id: 'inv_new' });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /invite member/i }));

    const emailInput = await screen.findByPlaceholderText(/colleague@company\.com/i);
    await user.type(emailInput, 'newbie@example.com');

    // Role is a Dropdown now (not a native select): open it, pick Admin.
    await user.click(screen.getByRole('button', { name: /^Member$/ }));
    await user.click(await screen.findByRole('menuitem', { name: 'Admin' }));

    await user.click(screen.getByRole('button', { name: /send invite/i }));

    await waitFor(() => {
      expect(invitePosted).toBeDefined();
    });
    const body = JSON.parse(String(invitePosted!.body));
    expect(body).toEqual({ email: 'newbie@example.com', role: 'admin' });

    // fetchMock was called with the POST request
    const postCall = fetchMock.mock.calls.find(
      ([u, i]) =>
        typeof u === 'string' && u.endsWith('/api/v1/org/invite') && i?.method === 'POST',
    );
    expect(postCall).toBeDefined();
  });

  it('invite error surfaces the server error message', async () => {
    stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org')) {
        return jsonOk({
          org: { id: 'org_x', name: 'SessionFS', slug: 'sessionfs', tier: 'enterprise', seats_used: 1, seats_limit: 100 },
          members: [
            { user_id: 'u_1', email: 'admin@example.com', display_name: null, role: 'admin', joined_at: '2026-03-01T12:00:00Z' },
          ],
          current_user_role: 'admin',
        });
      }
      if (url.endsWith('/api/v1/org/invites')) return jsonOk({ invites: [] });
      if (url.endsWith('/api/v1/org/invite') && init?.method === 'POST') {
        return jsonError(400, { detail: 'Seat limit reached' });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /invite member/i }));
    await user.type(await screen.findByPlaceholderText(/colleague/i), 'x@example.com');
    await user.click(screen.getByRole('button', { name: /send invite/i }));

    await waitFor(() => {
      expect(screen.getByText(/seat limit reached/i)).toBeInTheDocument();
    });
  });

  function adminOrg(extraMembers: object[] = []) {
    return (url: string) => {
      if (url.endsWith('/api/v1/org')) {
        return jsonOk({
          org: { id: 'org_x', name: 'SessionFS', slug: 'sessionfs', tier: 'enterprise', seats_used: 2, seats_limit: 100 },
          members: [
            { user_id: 'u_1', email: 'admin@example.com', display_name: null, role: 'admin', joined_at: '2026-03-01T12:00:00Z' },
            ...extraMembers,
          ],
          current_user_role: 'admin',
        });
      }
      if (url.endsWith('/api/v1/org/invites')) return jsonOk({ invites: [] });
      throw new Error(`unexpected url: ${url}`);
    };
  }

  it('shows the tab named in ?tab= and falls back to Members for unknown values', async () => {
    stubFetch(adminOrg());
    renderPage('/settings/organization?tab=security');
    expect(await screen.findByTestId('sso-stub-org_x')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Security' })).toHaveAttribute('aria-selected', 'true');
  });

  it('unknown ?tab= value selects Members', async () => {
    stubFetch(adminOrg());
    renderPage('/settings/organization?tab=bogus');
    expect(await screen.findByText('admin@example.com')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Members' })).toHaveAttribute('aria-selected', 'true');
  });

  it('?org= pointing at a secondary org renders that org instead of the primary', async () => {
    stubFetch(adminOrg());
    renderPage('/settings/organization?tab=personas&org=org_secondary');
    expect(await screen.findByTestId('secondary-org-stub-org_secondary')).toBeInTheDocument();
    expect(screen.queryByTestId('org-personas-stub-org_x')).not.toBeInTheDocument();
  });

  it('?org= matching the primary org id stays on the primary page', async () => {
    stubFetch(adminOrg());
    renderPage('/settings/organization?tab=personas&org=org_x');
    expect(await screen.findByTestId('org-personas-stub-org_x')).toBeInTheDocument();
    expect(screen.queryByTestId('secondary-org-stub-org_x')).not.toBeInTheDocument();
  });

  it('removing a member asks for confirmation in a dialog before the DELETE', async () => {
    const member = { user_id: 'u_2', email: 'member@example.com', display_name: null, role: 'member', joined_at: '2026-03-02T12:00:00Z' };
    const base = adminOrg([member]);
    let deleted = false;
    stubFetch((url, init) => {
      if (url.endsWith('/api/v1/org/members/u_2') && init?.method === 'DELETE') {
        deleted = true;
        return jsonOk({});
      }
      return base(url);
    });
    const confirmSpy = vi.spyOn(window, 'confirm');
    renderPage();
    const user = userEvent.setup();
    const removeButtons = await screen.findAllByRole('button', { name: 'Remove' });
    await user.click(removeButtons[removeButtons.length - 1]);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Remove member@example.com from the organization?');
    expect(deleted).toBe(false);
    await user.click(within(dialog).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(deleted).toBe(true));
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
