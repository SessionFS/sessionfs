import { useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import type { AdvanceAssignmentInput } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { useMe } from './useMe';

/**
 * Install disclosure + update diff (marketplace v2 §3). Query keys carry the
 * signed-in user id: what a version discloses depends on what the identity
 * can see.
 */
export function useMarketplaceDisclosure(
  versionId: string | undefined,
  tools: string[] = [],
  scope: 'project' | 'user' = 'project',
  enabled = true,
) {
  const { auth } = useAuth();
  const me = useMe();
  const identity = me.data?.user_id ?? null;
  const toolKey = [...tools].sort().join(',');
  return useQuery({
    queryKey: ['marketplace-disclosure', identity, versionId, toolKey, scope],
    queryFn: () => auth!.client.getMarketplaceDisclosure(versionId!, tools, scope),
    enabled: !!auth && !!identity && !!versionId && enabled,
    staleTime: 5 * 60_000,
  });
}

export function useMarketplaceVersionDiff(
  fromVersionId: string | undefined,
  toVersionId: string | undefined,
  enabled = true,
) {
  const { auth } = useAuth();
  const me = useMe();
  const identity = me.data?.user_id ?? null;
  return useQuery({
    queryKey: ['marketplace-diff', identity, fromVersionId, toVersionId],
    queryFn: () => auth!.client.getMarketplaceVersionDiff(fromVersionId!, toVersionId!),
    enabled: !!auth && !!identity && !!fromVersionId && !!toVersionId && enabled,
    staleTime: 5 * 60_000,
  });
}

export const HELD_UPDATES_PAGE_SIZE = 20;

/** The org's held-updates queue (keyset-paginated; owners/admins). */
export function useHeldUpdates(orgId: string | undefined, enabled = true) {
  const { auth } = useAuth();
  const me = useMe();
  const identity = me.data?.user_id ?? null;
  return useInfiniteQuery({
    queryKey: ['held-updates', identity, orgId],
    queryFn: ({ pageParam }) =>
      auth!.client.listHeldUpdates(orgId!, { cursor: pageParam || undefined, limit: HELD_UPDATES_PAGE_SIZE }),
    initialPageParam: '' as string,
    getNextPageParam: (last) => (last.has_more && last.next_cursor ? last.next_cursor : undefined),
    enabled: !!auth && !!identity && !!orgId && enabled,
    staleTime: 30_000,
  });
}

export function useAdvanceAssignment(orgId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ assignmentId, input }: { assignmentId: string; input: AdvanceAssignmentInput }) =>
      auth!.client.advanceAssignment(orgId!, assignmentId, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['held-updates'] });
      void qc.invalidateQueries({ queryKey: ['skill-assignments-effective'] });
    },
  });
}

/** Held-update counts for several orgs, summed, from the cheap count
 *  endpoint (no diffs are built). `more` is true when any org's count is
 *  capped. A failed org (for example below the Team plan) counts as 0 and is
 *  listed in `errors`, so `loaded` still settles. Keys sit under
 *  ['held-updates'] so an advance refreshes them too. */
export function useHeldUpdateCounts(orgIds: string[]) {
  const { auth } = useAuth();
  const me = useMe();
  const identity = me.data?.user_id ?? null;
  const results = useQueries({
    queries: orgIds.map((orgId) => ({
      queryKey: ['held-updates', identity, orgId, 'count'],
      queryFn: () => auth!.client.countHeldUpdates(orgId),
      enabled: !!auth && !!identity,
      staleTime: 30_000,
      retry: false,
    })),
  });
  const byOrg: Record<string, number> = {};
  const errors: Record<string, unknown> = {};
  let total = 0;
  let more = false;
  results.forEach((result, index) => {
    const orgId = orgIds[index];
    if (result.error) errors[orgId] = result.error;
    const count = result.data?.count ?? 0;
    byOrg[orgId] = count;
    total += count;
    more = more || !!result.data?.has_more;
  });
  const loaded = results.length > 0 && results.every((result) => !!result.data || !!result.error);
  return { total, more, byOrg, errors, loaded };
}
