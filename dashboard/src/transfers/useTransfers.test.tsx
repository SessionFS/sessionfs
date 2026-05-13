/**
 * Hook-level tests for v0.10.0 Phase 4 useTransfers mutations.
 *
 * The component tests (TransferInbox.test.tsx / TransferPanel.test.tsx)
 * mock the hooks entirely so they can't catch cache-invalidation bugs.
 * These tests wire a real QueryClient + AuthProvider and assert that
 * successful transfer mutations invalidate BOTH `['transfers']` AND
 * `['project']` — closing the Round 3 Codex finding (KB entry 280)
 * where `useProject(['project', remote])` stayed stale up to its 60s
 * staleTime after an auto-accepted transfer.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    auth: { apiKey: 'k', baseUrl: 'http://api.test' },
  }),
}));

import {
  useAcceptTransfer,
  useCancelTransfer,
  useInitiateTransfer,
  useRejectTransfer,
} from './useTransfers';

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

function makeOkResponse(body: object) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

const fakeTransfer = {
  id: 'xfer_x',
  project_id: 'proj_x',
  project_git_remote_snapshot: 'github.com/acme/x',
  project_name_snapshot: 'acme-x',
  initiated_by: 'u_me',
  target_user_id: 'u_me',
  from_scope: 'personal',
  to_scope: 'org_acme',
  state: 'accepted',
  accepted_by: 'u_me',
  created_at: '2026-05-12T00:00:00Z',
  accepted_at: '2026-05-12T00:00:00Z',
  updated_at: '2026-05-12T00:00:00Z',
};

beforeEach(() => {
  vi.spyOn(global, 'fetch').mockResolvedValue(makeOkResponse(fakeTransfer));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('useTransfers hooks — cache invalidation', () => {
  it('useInitiateTransfer success invalidates both transfers and project caches', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const wrapper = makeWrapper(qc);

    const { result } = renderHook(() => useInitiateTransfer('proj_x'), { wrapper });
    await act(async () => {
      result.current.mutate({ to: 'org_acme' });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(['transfers']);
    expect(keys).toContainEqual(['project']);
  });

  it('useAcceptTransfer success invalidates both transfers and project caches', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const wrapper = makeWrapper(qc);

    const { result } = renderHook(() => useAcceptTransfer(), { wrapper });
    await act(async () => {
      result.current.mutate({ transferId: 'xfer_x' });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(['transfers']);
    expect(keys).toContainEqual(['project']);
  });

  it('useRejectTransfer success invalidates both caches', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const wrapper = makeWrapper(qc);

    const { result } = renderHook(() => useRejectTransfer(), { wrapper });
    await act(async () => {
      result.current.mutate({ transferId: 'xfer_x' });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(['transfers']);
    expect(keys).toContainEqual(['project']);
  });

  it('useCancelTransfer success invalidates both caches', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, 'invalidateQueries');
    const wrapper = makeWrapper(qc);

    const { result } = renderHook(() => useCancelTransfer(), { wrapper });
    await act(async () => {
      result.current.mutate({ transferId: 'xfer_x' });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(['transfers']);
    expect(keys).toContainEqual(['project']);
  });
});
