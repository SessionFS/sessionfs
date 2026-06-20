import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ActivateLicensePanel from './ActivateLicensePanel';

/**
 * UI coverage for the self-service license activation flow (v0.11.0).
 * Drives raw fetch against:
 *   GET  /api/v1/org/activate/info?key=…
 *   POST /api/v1/org/activate
 *   POST /api/v1/org/activate/verify
 */

const { mockAuth } = vi.hoisted(() => ({ mockAuth: vi.fn() }));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function renderPanel() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <ActivateLicensePanel />
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
function jsonError(status: number, body: Record<string, unknown> = {}): Response {
  return { ok: false, status, json: async () => body } as unknown as Response;
}

describe('ActivateLicensePanel', () => {
  beforeEach(() => {
    mockAuth.mockReset();
    mockAuth.mockReturnValue({ auth: { apiKey: 'sk_test', baseUrl: 'http://test.api' } });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('walks the full key → review → code → done flow', async () => {
    let activatePosted: RequestInit | undefined;
    let verifyPosted: RequestInit | undefined;
    stubFetch((url, init) => {
      if (url.includes('/api/v1/org/activate/info')) {
        expect(url).toContain('key=SFS-AAAA');
        return jsonOk({ valid: true, org_name: 'Acme Corp', tier: 'enterprise' });
      }
      if (url.endsWith('/api/v1/org/activate') && init?.method === 'POST') {
        activatePosted = init;
        return jsonOk({ status: 'verification_sent', message: 'sent' });
      }
      if (url.endsWith('/api/v1/org/activate/verify') && init?.method === 'POST') {
        verifyPosted = init;
        return jsonOk({ org_id: 'org_new', name: 'Acme Corp', slug: 'acme-corp', tier: 'enterprise', seats_limit: 50 });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPanel();
    const user = userEvent.setup();

    // Step 1 — key lookup
    await user.type(screen.getByLabelText(/license key/i), 'SFS-AAAA');
    await user.click(screen.getByRole('button', { name: /look up license/i }));

    // Step 2 — review
    await waitFor(() => expect(screen.getByText('Acme Corp')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /^activate$/i }));

    // activate POST carried the key
    await waitFor(() => expect(activatePosted).toBeDefined());
    expect(JSON.parse(String(activatePosted!.body))).toMatchObject({ key: 'SFS-AAAA' });

    // Step 3 — code entry
    await waitFor(() => expect(screen.getByLabelText(/verification code/i)).toBeInTheDocument());
    await user.type(screen.getByLabelText(/verification code/i), 'CODE123456');
    await user.click(screen.getByRole('button', { name: /verify & activate/i }));

    // verify POST carried the token
    await waitFor(() => expect(verifyPosted).toBeDefined());
    expect(JSON.parse(String(verifyPosted!.body))).toEqual({ token: 'CODE123456' });

    // Step 4 — success
    await waitFor(() => expect(screen.getByText(/acme corp is activated/i)).toBeInTheDocument());
    expect(screen.getByText(/you're the/i)).toHaveTextContent(/owner/i);
  });

  it('shows a non-oracular error for an invalid key', async () => {
    stubFetch((url) => {
      if (url.includes('/api/v1/org/activate/info')) return jsonOk({ valid: false });
      throw new Error(`unexpected url: ${url}`);
    });

    renderPanel();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/license key/i), 'SFS-BAD');
    await user.click(screen.getByRole('button', { name: /look up license/i }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/invalid or not available/i),
    );
    // Did NOT advance to review.
    expect(screen.queryByRole('button', { name: /^activate$/i })).not.toBeInTheDocument();
  });

  it('skips the code step when Phase A returns the activated org (exact-match shortcut)', async () => {
    stubFetch((url, init) => {
      if (url.includes('/api/v1/org/activate/info')) {
        return jsonOk({ valid: true, org_name: 'Shortcut Inc', tier: 'team' });
      }
      if (url.endsWith('/api/v1/org/activate') && init?.method === 'POST') {
        return jsonOk({ org_id: 'org_sc', name: 'Shortcut Inc', slug: 'shortcut-inc', tier: 'team', seats_limit: 10 });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPanel();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/license key/i), 'SFS-SC');
    await user.click(screen.getByRole('button', { name: /look up license/i }));
    await waitFor(() => expect(screen.getByText('Shortcut Inc')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /^activate$/i }));

    // Went straight to success, never asked for a code.
    await waitFor(() => expect(screen.getByText(/shortcut inc is activated/i)).toBeInTheDocument());
    expect(screen.queryByLabelText(/verification code/i)).not.toBeInTheDocument();
  });

  it('surfaces an expired/used code error from Phase B', async () => {
    stubFetch((url, init) => {
      if (url.includes('/api/v1/org/activate/info')) return jsonOk({ valid: true, org_name: 'Acme', tier: 'team' });
      if (url.endsWith('/api/v1/org/activate') && init?.method === 'POST') {
        return jsonOk({ status: 'verification_sent' });
      }
      if (url.endsWith('/api/v1/org/activate/verify') && init?.method === 'POST') {
        return jsonError(410, { detail: 'The verification code is invalid, expired, or has already been used.' });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    renderPanel();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/license key/i), 'SFS-X');
    await user.click(screen.getByRole('button', { name: /look up license/i }));
    await waitFor(() => expect(screen.getByText('Acme')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /^activate$/i }));
    await waitFor(() => expect(screen.getByLabelText(/verification code/i)).toBeInTheDocument());
    await user.type(screen.getByLabelText(/verification code/i), 'STALE');
    await user.click(screen.getByRole('button', { name: /verify & activate/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/expired, or has already been used/i));
  });
});
