import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { ApiError } from '../api/client';

const { mocks } = vi.hoisted(() => ({
  mocks: { useAuth: vi.fn(), useMe: vi.fn() },
}));
vi.mock('../auth/AuthContext', () => ({ useAuth: mocks.useAuth }));
vi.mock('./useMe', () => ({ useMe: mocks.useMe }));

import { useHeldUpdateCounts } from './useMarketplaceDisclosure';

function wrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe('useHeldUpdateCounts', () => {
  const countHeldUpdates = vi.fn();
  const listHeldUpdates = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.useMe.mockReturnValue({ data: { user_id: 'user_1' } });
    mocks.useAuth.mockReturnValue({ auth: { client: { countHeldUpdates, listHeldUpdates } } });
  });

  it('sums the count endpoint across orgs and flags a capped one', async () => {
    countHeldUpdates.mockImplementation(async (orgId: string) =>
      orgId === 'org_a' ? { count: 2, has_more: false } : { count: 100, has_more: true });
    const { result } = renderHook(() => useHeldUpdateCounts(['org_a', 'org_b']), { wrapper: wrapper(new QueryClient()) });
    expect(result.current.loaded).toBe(false);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.total).toBe(102);
    expect(result.current.more).toBe(true);
    expect(result.current.byOrg).toEqual({ org_a: 2, org_b: 100 });
    expect(result.current.errors).toEqual({});
    // The cheap count endpoint only: no diff-building queue request.
    expect(countHeldUpdates).toHaveBeenCalledTimes(2);
    expect(listHeldUpdates).not.toHaveBeenCalled();
  });

  it('settles with a gated org counted as 0 and reported in errors', async () => {
    const gate = new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'Team plan required' } }));
    countHeldUpdates.mockImplementation(async (orgId: string) => {
      if (orgId === 'org_free') throw gate;
      return { count: 3, has_more: false };
    });
    const { result } = renderHook(() => useHeldUpdateCounts(['org_team', 'org_free']), { wrapper: wrapper(new QueryClient()) });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.total).toBe(3);
    expect(result.current.more).toBe(false);
    expect(result.current.byOrg).toEqual({ org_team: 3, org_free: 0 });
    expect(result.current.errors.org_free).toBe(gate);
    expect(result.current.errors.org_team).toBeUndefined();
  });
});
