/**
 * P1-4 regression: marketplace query keys are identity-scoped and logout
 * clears the QueryClient, so user B never sees user A's private cached rows.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

const { mocks } = vi.hoisted(() => ({
  mocks: { useAuth: vi.fn(), useMe: vi.fn() },
}));
vi.mock('../auth/AuthContext', () => ({ useAuth: mocks.useAuth }));
vi.mock('./useMe', () => ({ useMe: mocks.useMe }));

import { useDeletePersonalSkillVersion, useMarketplacePackages, useSkillInstallRequests } from './useMarketplace';

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe('useMarketplace identity scoping', () => {
  const searchMarketplace = vi.fn();
  const listSkillInstallRequests = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    searchMarketplace.mockResolvedValue({ items: [{ listing_id: 'private-of-A' }], count: 1, has_more: false, next_cursor: null });
    listSkillInstallRequests.mockResolvedValue({ items: [], can_approve: false });
    mocks.useAuth.mockReturnValue({ auth: { client: { searchMarketplace, listSkillInstallRequests } } });
  });

  it('keys marketplace queries on the signed-in user id and stays disabled until it is known', async () => {
    const qc = new QueryClient();
    mocks.useMe.mockReturnValue({ data: undefined });
    const { result, rerender } = renderHook(() => useMarketplacePackages('review', ''), { wrapper: makeWrapper(qc) });
    expect(result.current.fetchStatus).toBe('idle');
    expect(searchMarketplace).not.toHaveBeenCalled();

    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
    rerender();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(qc.getQueryData(['marketplace', 'user_A', 'review', '', {}])).toEqual({
      pages: [{ items: [{ listing_id: 'private-of-A' }], count: 1, has_more: false, next_cursor: null }],
      pageParams: [''],
    });

    // A second identity gets a different key: never served A's cached rows.
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_B' } });
    searchMarketplace.mockResolvedValue({ items: [], count: 0, has_more: false, next_cursor: null });
    rerender();
    await waitFor(() => expect(result.current.data?.pages[0]?.count).toBe(0));
    expect(qc.getQueryData(['marketplace', 'user_B', 'review', '', {}])).toEqual({
      pages: [{ items: [], count: 0, has_more: false, next_cursor: null }],
      pageParams: [''],
    });
    expect(qc.getQueryData(['marketplace', 'review', ''])).toBeUndefined();
  });

  it('pages through the catalog with the server cursor and never re-sends the first page', async () => {
    const qc = new QueryClient();
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
    searchMarketplace
      .mockResolvedValueOnce({ items: [{ listing_id: 'a' }], count: 1, has_more: true, next_cursor: 'cur-1' })
      .mockResolvedValueOnce({ items: [{ listing_id: 'b' }], count: 1, has_more: false, next_cursor: null });
    const { result } = renderHook(() => useMarketplacePackages('', 'codex'), { wrapper: makeWrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(searchMarketplace).toHaveBeenLastCalledWith({ q: undefined, tool: 'codex', limit: 24, cursor: undefined });
    expect(result.current.hasNextPage).toBe(true);
    await act(async () => { await result.current.fetchNextPage(); });
    expect(searchMarketplace).toHaveBeenLastCalledWith({ q: undefined, tool: 'codex', limit: 24, cursor: 'cur-1' });
    await waitFor(() =>
      expect(result.current.data?.pages.flatMap((page) => page.items.map((item) => item.listing_id))).toEqual(['a', 'b']),
    );
    expect(result.current.hasNextPage).toBe(false);
    expect(searchMarketplace).toHaveBeenCalledTimes(2);
  });

  it('scopes install-request queries on identity too', async () => {
    const qc = new QueryClient();
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
    const { result } = renderHook(() => useSkillInstallRequests('prj_1'), { wrapper: makeWrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(qc.getQueryCache().find({ queryKey: ['skill-install-requests', 'user_A', 'prj_1'] })).toBeTruthy();
  });

  it('login with a new key clears every cached query', async () => {
    const qc = new QueryClient();
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
    const { result } = renderHook(() => useMarketplacePackages('', ''), { wrapper: makeWrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(qc.getQueryCache().getAll().length).toBeGreaterThan(0);

    vi.doUnmock('../auth/AuthContext');
    vi.resetModules();
    vi.doMock('../api/client', () => ({
      createApiClient: () => ({ health: vi.fn().mockResolvedValue({ status: 'ok' }) }),
    }));
    const { AuthProvider, useAuth } = await import('../auth/AuthContext');
    const authHook = renderHook(() => useAuth(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={qc}><AuthProvider>{children}</AuthProvider></QueryClientProvider>
      ),
    });
    await act(async () => { await authHook.result.current.login('https://api.example', 'sk-new'); });
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
    vi.doUnmock('../api/client');
  });

  it('deleting a version resolves on 204 and invalidates marketplace queries', async () => {
    const qc = new QueryClient();
    const deletePersonalSkillVersion = vi.fn().mockResolvedValue(undefined);
    mocks.useAuth.mockReturnValue({ auth: { client: { searchMarketplace, listSkillInstallRequests, deletePersonalSkillVersion } } });
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useDeletePersonalSkillVersion(), { wrapper: makeWrapper(qc) });
    await act(async () => {
      await expect(result.current.mutateAsync({ packageId: 'spk_1', versionId: 'skv_1' })).resolves.toBeUndefined();
    });
    expect(deletePersonalSkillVersion).toHaveBeenCalledWith('spk_1', 'skv_1');
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['marketplace'] });
  });

  it('logout clears every cached query', async () => {
    const qc = new QueryClient();
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
    const { result } = renderHook(() => useMarketplacePackages('', ''), { wrapper: makeWrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(qc.getQueryCache().getAll().length).toBeGreaterThan(0);

    vi.doUnmock('../auth/AuthContext');
    vi.resetModules();
    const { AuthProvider, useAuth } = await import('../auth/AuthContext');
    const authHook = renderHook(() => useAuth(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={qc}><AuthProvider>{children}</AuthProvider></QueryClientProvider>
      ),
    });
    act(() => authHook.result.current.logout());
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
  });
});
