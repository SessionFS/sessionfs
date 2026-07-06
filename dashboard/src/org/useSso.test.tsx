/**
 * Unit tests for the SSO hooks in useSso.ts.
 * Verifies correct URL + method + query-key invalidation via mocked fetch.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import {
  useSsoProvider,
  useCreateProvider,
  useUpdateProvider,
  useDeleteProvider,
  useDomains,
  useCreateDomain,
  useVerifyDomain,
  useDeleteDomain,
  useToggleEnforcement,
  useBreakGlassGrants,
  useCreateBreakGlass,
  useRevokeBreakGlass,
} from './useSso';

/* ── mocks ── */

const { authMock } = vi.hoisted(() => ({
  authMock: {
    useAuth: vi.fn(),
  },
}));

vi.mock('../auth/AuthContext', () => authMock);

const BASE = 'https://api.example.com';
const API_KEY = 'sk-test-123';
const ORG_ID = 'org_abc';

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.resetAllMocks();
  authMock.useAuth.mockReturnValue({
    auth: { apiKey: API_KEY, baseUrl: BASE },
  });
});

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  });
}

/* ── tests ── */

describe('useSsoProvider', () => {
  it('calls GET /provider and returns data', async () => {
    const data = { id: 'p1', display_name: 'Okta', issuer: 'https://okta.example.com', client_id: 'cid', client_secret_ref: 'env:OKTA_SECRET', allowed_scopes: ['openid'], enabled: true, enforced: false };
    globalThis.fetch = mockFetch(200, data);
    const { result } = renderHook(() => useSsoProvider(ORG_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/provider`,
      expect.any(Object),
    );
    expect(result.current.data).toEqual(data);
  });

  it('returns null on 404', async () => {
    globalThis.fetch = mockFetch(404, '{}');
    const { result } = renderHook(() => useSsoProvider(ORG_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
  });

  it('is disabled when orgId is undefined', () => {
    globalThis.fetch = mockFetch(200, {});
    const { result } = renderHook(() => useSsoProvider(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
  });
});

describe('useCreateProvider', () => {
  it('POSTs to /provider and invalidates query', async () => {
    const data = { id: 'p1', display_name: 'Okta' };
    globalThis.fetch = mockFetch(201, data);
    const { result } = renderHook(() => useCreateProvider(ORG_ID), { wrapper });
    result.current.mutate({ display_name: 'Okta', issuer: 'https://okta.example.com', client_id: 'cid', client_secret_ref: 'env:SECRET', allowed_scopes: ['openid'] });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/provider`,
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('useUpdateProvider', () => {
  it('PATCHes /provider and invalidates provider + domains', async () => {
    globalThis.fetch = mockFetch(200, { id: 'p1' });
    const { result } = renderHook(() => useUpdateProvider(ORG_ID), { wrapper });
    result.current.mutate({ enabled: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/provider`,
      expect.objectContaining({ method: 'PATCH' }),
    );
  });
});

describe('useDeleteProvider', () => {
  it('DELETEs /provider and invalidates provider + domains', async () => {
    globalThis.fetch = mockFetch(204, null);
    const { result } = renderHook(() => useDeleteProvider(ORG_ID), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/provider`,
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});

describe('useDomains', () => {
  it('calls GET /domains', async () => {
    const data = [{ id: 'd1', domain: 'example.com', status: 'verified' }];
    globalThis.fetch = mockFetch(200, data);
    const { result } = renderHook(() => useDomains(ORG_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/domains`,
      expect.any(Object),
    );
    expect(result.current.data).toEqual(data);
  });
});

describe('useCreateDomain', () => {
  it('POSTs to /domains with domain body', async () => {
    globalThis.fetch = mockFetch(201, { id: 'd1', domain: 'example.com', txt_record: 'TXT=abc' });
    const { result } = renderHook(() => useCreateDomain(ORG_ID), { wrapper });
    result.current.mutate({ domain: 'example.com' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/domains`,
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('useVerifyDomain', () => {
  it('POSTs to /domains/:id/verify', async () => {
    globalThis.fetch = mockFetch(200, { id: 'd1', domain: 'example.com', status: 'verified' });
    const { result } = renderHook(() => useVerifyDomain(ORG_ID), { wrapper });
    result.current.mutate('d1');
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/domains/d1/verify`,
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('useDeleteDomain', () => {
  it('DELETEs /domains/:id', async () => {
    globalThis.fetch = mockFetch(204, null);
    const { result } = renderHook(() => useDeleteDomain(ORG_ID), { wrapper });
    result.current.mutate('d1');
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/domains/d1`,
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});

describe('useToggleEnforcement', () => {
  it('PATCHes /provider/enforcement with {enforced}', async () => {
    globalThis.fetch = mockFetch(200, { enforced: true });
    const { result } = renderHook(() => useToggleEnforcement(ORG_ID), { wrapper });
    result.current.mutate(true);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/provider/enforcement`,
      expect.objectContaining({ method: 'PATCH' }),
    );
  });
});

describe('useBreakGlassGrants', () => {
  it('calls GET /break-glass', async () => {
    const data = [{ id: 'g1', admin_user_id: 'u1', expires_at: '2099-01-01', revoked_at: null }];
    globalThis.fetch = mockFetch(200, data);
    const { result } = renderHook(() => useBreakGlassGrants(ORG_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/break-glass`,
      expect.any(Object),
    );
    expect(result.current.data).toEqual(data);
  });
});

describe('useCreateBreakGlass', () => {
  it('POSTs to /break-glass with admin_user_id', async () => {
    globalThis.fetch = mockFetch(201, { id: 'g1', admin_user_id: 'u1' });
    const { result } = renderHook(() => useCreateBreakGlass(ORG_ID), { wrapper });
    result.current.mutate({ admin_user_id: 'u1' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/break-glass`,
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('useRevokeBreakGlass', () => {
  it('DELETEs /break-glass/:id', async () => {
    globalThis.fetch = mockFetch(204, null);
    const { result } = renderHook(() => useRevokeBreakGlass(ORG_ID), { wrapper });
    result.current.mutate('g1');
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${BASE}/api/v1/orgs/${ORG_ID}/sso/break-glass/g1`,
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});
