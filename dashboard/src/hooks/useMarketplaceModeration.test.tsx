import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

const { mocks } = vi.hoisted(() => ({
  mocks: { useAuth: vi.fn(), useMe: vi.fn() },
}));
vi.mock('../auth/AuthContext', () => ({ useAuth: mocks.useAuth }));
vi.mock('./useMe', () => ({ useMe: mocks.useMe }));

import {
  REVOCATION_FEED_MAX_PAGES,
  useMarketplaceReports,
  useModerateMarketplace,
  useRevocationIndex,
} from './useMarketplace';
import { isRevocationFeedIncomplete } from '../marketplace/moderationState';

function wrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

function event(n: number, state: 'yanked' | 'blocked' | 'restored', versionId: string | null = `v${n}`) {
  return { package_id: 'p1', version_id: versionId, content_hash: null, state, reason: 'r', changed_at: `t${n}` };
}

describe('marketplace moderation hooks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_A' } });
  });

  it('pages through the revocations feed with next_since and replays it', async () => {
    const full = Array.from({ length: 500 }, (_, i) => event(i, 'yanked'));
    const listMarketplaceRevocations = vi.fn()
      .mockResolvedValueOnce({ revocations: full, next_since: 't499' })
      .mockResolvedValueOnce({ revocations: [event(0, 'restored', 'v0'), event(1, 'blocked', null)], next_since: 'tz', has_more: false });
    mocks.useAuth.mockReturnValue({ auth: { client: { listMarketplaceRevocations } } });
    const qc = new QueryClient();
    const { result } = renderHook(() => useRevocationIndex(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(listMarketplaceRevocations).toHaveBeenNthCalledWith(1, { since: undefined, limit: 500 });
    expect(listMarketplaceRevocations).toHaveBeenNthCalledWith(2, { since: 't499', limit: 500 });
    expect(listMarketplaceRevocations).toHaveBeenCalledTimes(2);
    expect(result.current.data?.versions.get('v0')).toBe('active');
    expect(result.current.data?.versions.get('v7')).toBe('yanked');
    expect(result.current.data?.packages.get('p1')).toBe('blocked');
    expect(qc.getQueryData(['marketplace-revocations', 'user_A'])).toBeDefined();
  });

  it('keeps paging while has_more is true, well past the old 5,000-event limit', async () => {
    let calls = 0;
    const listMarketplaceRevocations = vi.fn(async () => {
      calls += 1;
      return { revocations: [event(calls, 'yanked')], next_since: `t${calls}`, has_more: calls < 15 };
    });
    mocks.useAuth.mockReturnValue({ auth: { client: { listMarketplaceRevocations } } });
    const { result } = renderHook(() => useRevocationIndex(), { wrapper: wrapper(new QueryClient()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(listMarketplaceRevocations).toHaveBeenCalledTimes(15);
    expect(result.current.data?.versions.get('v15')).toBe('yanked');
  });

  it('fails the query on a truncated replay instead of caching it, and does not retry', async () => {
    let calls = 0;
    const listMarketplaceRevocations = vi.fn(async () => {
      calls += 1;
      return { revocations: [event(calls, 'yanked')], next_since: `t${calls}`, has_more: true };
    });
    mocks.useAuth.mockReturnValue({ auth: { client: { listMarketplaceRevocations } } });
    const qc = new QueryClient();
    const { result } = renderHook(() => useRevocationIndex(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(listMarketplaceRevocations).toHaveBeenCalledTimes(REVOCATION_FEED_MAX_PAGES);
    expect(isRevocationFeedIncomplete(result.current.error)).toBe(true);
    expect(result.current.data).toBeUndefined();
    expect(qc.getQueryData(['marketplace-revocations', 'user_A'])).toBeUndefined();
  });

  it('keeps the previous COMPLETE index when a later refresh is truncated', async () => {
    let truncated = false;
    let calls = 0;
    const listMarketplaceRevocations = vi.fn(async () => {
      calls += 1;
      if (!truncated) return { revocations: [event(1, 'blocked')], next_since: 't1', has_more: false };
      return { revocations: [event(100 + calls, 'restored', 'v1')], next_since: `t${100 + calls}`, has_more: true };
    });
    mocks.useAuth.mockReturnValue({ auth: { client: { listMarketplaceRevocations } } });
    const qc = new QueryClient();
    const { result } = renderHook(() => useRevocationIndex(), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    truncated = true;
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    // The partial replay (which would have "restored" v1) is never used.
    expect(result.current.data?.versions.get('v1')).toBe('blocked');
    expect(isRevocationFeedIncomplete(result.current.error)).toBe(true);
  });

  it('reads an older server (no has_more) until an empty page', async () => {
    const listMarketplaceRevocations = vi.fn()
      .mockResolvedValueOnce({ revocations: [event(1, 'yanked')], next_since: 't1' })
      .mockResolvedValueOnce({ revocations: [event(2, 'blocked')], next_since: 't2' })
      .mockResolvedValueOnce({ revocations: [], next_since: 't2' });
    mocks.useAuth.mockReturnValue({ auth: { client: { listMarketplaceRevocations } } });
    const { result } = renderHook(() => useRevocationIndex(), { wrapper: wrapper(new QueryClient()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(listMarketplaceRevocations).toHaveBeenCalledTimes(3);
    expect(result.current.data?.versions.get('v2')).toBe('blocked');
  });

  it('keeps the moderation queue disabled for non-moderators', () => {
    const listMarketplaceReports = vi.fn();
    mocks.useAuth.mockReturnValue({ auth: { client: { listMarketplaceReports } } });
    const { result } = renderHook(() => useMarketplaceReports('open', false), { wrapper: wrapper(new QueryClient()) });
    expect(result.current.fetchStatus).toBe('idle');
    expect(listMarketplaceReports).not.toHaveBeenCalled();
  });

  it('refreshes reports, the feed and the catalog after a moderation action', async () => {
    const moderateMarketplace = vi.fn().mockResolvedValue({ state: 'yanked' });
    mocks.useAuth.mockReturnValue({ auth: { client: { moderateMarketplace } } });
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    const { result } = renderHook(() => useModerateMarketplace(), { wrapper: wrapper(qc) });
    await act(async () => {
      await result.current.mutateAsync({ kind: 'packages', targetId: 'spk_1', action: 'yank', reason: 'r' });
    });
    expect(moderateMarketplace).toHaveBeenCalledWith('packages', 'spk_1', 'yank', 'r');
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['marketplace-reports'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['marketplace-revocations'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['marketplace'] });
  });
});
