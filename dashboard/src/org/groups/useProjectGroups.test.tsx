import type { ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  groupKeys,
  useAddGroupProject,
  useCreateGroup,
  useDeleteGroup,
  useGrantGroupMember,
  useMyGroups,
  useOrgGroups,
  useProjectGroup,
  useRemoveGroupProject,
  useRevokeGroupMember,
  useUpdateGroup,
} from './useProjectGroups';
import { ORG, authState, detail, makeClient, summary } from './groupTestHarness';

vi.mock('../../auth/AuthContext', async () => (await import('./groupTestHarness')).authModule);

let qc: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
);

beforeEach(() => {
  authState.client = makeClient();
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
});

describe('project group queries', () => {
  it('lists an org’s groups and the caller’s groups', async () => {
    const org = renderHook(() => useOrgGroups(ORG), { wrapper });
    const mine = renderHook(() => useMyGroups(), { wrapper });
    await waitFor(() => expect(org.result.current.data).toEqual([summary()]));
    await waitFor(() => expect(mine.result.current.data).toEqual([summary()]));
  });

  it('does not fetch without ids', () => {
    renderHook(() => useOrgGroups(undefined), { wrapper });
    renderHook(() => useProjectGroup(ORG, undefined), { wrapper });
    expect(authState.client.listOrgGroups).not.toHaveBeenCalled();
    expect(authState.client.getGroup).not.toHaveBeenCalled();
  });

  it('does not retry a 404 detail', async () => {
    authState.client.getGroup.mockRejectedValue(new Error('404'));
    const { result } = renderHook(() => useProjectGroup(ORG, 'pg_1'), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(authState.client.getGroup).toHaveBeenCalledTimes(1);
  });
});

describe('project group mutations', () => {
  function seedDependents() {
    for (const key of [groupKeys.org(ORG), groupKeys.mine(), groupKeys.project('proj_a'), ['projects'], ['project', 'proj_a']]) {
      qc.setQueryData(key, 'stale');
    }
  }
  function expectInvalidated() {
    for (const key of [groupKeys.org(ORG), groupKeys.mine(), groupKeys.project('proj_a'), ['projects'], ['project', 'proj_a']]) {
      expect(qc.getQueryState(key)?.isInvalidated).toBe(true);
    }
  }

  it('create seeds the new detail and invalidates group/project state', async () => {
    seedDependents();
    const { result } = renderHook(() => useCreateGroup(ORG), { wrapper });
    await act(() => result.current.mutateAsync({ name: 'New', slug: 'new' }));
    expect(authState.client.createGroup).toHaveBeenCalledWith(ORG, { name: 'New', slug: 'new' });
    expect(qc.getQueryData(groupKeys.detail(ORG, 'pg_new'))).toMatchObject({ id: 'pg_new' });
    expectInvalidated();
  });

  it.each([
    ['grant', () => useGrantGroupMember(ORG, 'pg_1'), { userId: 'u_x', role: 'editor' }, 'grantGroupMember', [ORG, 'pg_1', 'u_x', 'editor']],
    ['revoke', () => useRevokeGroupMember(ORG, 'pg_1'), 'u_x', 'revokeGroupMember', [ORG, 'pg_1', 'u_x']],
    ['add project', () => useAddGroupProject(ORG, 'pg_1'), 'proj_b', 'addGroupProject', [ORG, 'pg_1', 'proj_b']],
    ['remove project', () => useRemoveGroupProject(ORG, 'pg_1'), 'proj_a', 'removeGroupProject', [ORG, 'pg_1', 'proj_a']],
    ['update', () => useUpdateGroup(ORG, 'pg_1'), { name: 'X' }, 'updateGroup', [ORG, 'pg_1', { name: 'X' }]],
  ] as const)('%s writes the returned detail and invalidates', async (_label, hook, vars, method, args) => {
    const fresh = detail({ name: 'Fresh' });
    (authState.client[method] as ReturnType<typeof vi.fn>).mockResolvedValue(fresh);
    seedDependents();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const { result } = renderHook(hook as () => any, { wrapper });
    await act(() => result.current.mutateAsync(vars));
    expect(authState.client[method]).toHaveBeenCalledWith(...args);
    expect(qc.getQueryData(groupKeys.detail(ORG, 'pg_1'))).toEqual(fresh);
    // Also marked stale, so an overlapping mutation's older answer is refetched.
    expect(qc.getQueryState(groupKeys.detail(ORG, 'pg_1'))?.isInvalidated).toBe(true);
    expectInvalidated();
  });

  it('delete drops the cached detail', async () => {
    qc.setQueryData(groupKeys.detail(ORG, 'pg_1'), detail());
    seedDependents();
    const { result } = renderHook(() => useDeleteGroup(ORG, 'pg_1'), { wrapper });
    await act(() => result.current.mutateAsync());
    expect(authState.client.deleteGroup).toHaveBeenCalledWith(ORG, 'pg_1');
    expect(qc.getQueryData(groupKeys.detail(ORG, 'pg_1'))).toBeUndefined();
    expectInvalidated();
  });
});
