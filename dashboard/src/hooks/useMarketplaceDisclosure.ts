import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
