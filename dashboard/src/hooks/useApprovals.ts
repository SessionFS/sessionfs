import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { ApprovalLogFilters, ApprovalPolicyBody } from '../api/client';

/** React-Query hooks for approval policies, the approvals queue and requests. */

const POLICIES = 'approval-policies';
const ROOT = 'approvals';

export function useApprovalPolicies(orgId: string | undefined, includeInactive = false) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [POLICIES, orgId, includeInactive],
    queryFn: () => auth!.client.listApprovalPolicies(orgId!, { include_inactive: includeInactive }),
    enabled: !!auth && !!orgId,
    staleTime: 15_000,
  });
}

function usePolicyInvalidate() {
  const qc = useQueryClient();
  return () => void qc.invalidateQueries({ queryKey: [POLICIES] });
}

export function useSaveApprovalPolicy(orgId: string) {
  const { auth } = useAuth();
  const done = usePolicyInvalidate();
  return useMutation({
    mutationFn: (v: { policyId?: string; body: ApprovalPolicyBody }) =>
      v.policyId
        ? auth!.client.updateApprovalPolicy(orgId, v.policyId, v.body)
        : auth!.client.createApprovalPolicy(orgId, v.body),
    onSuccess: done,
  });
}

export function useDisableApprovalPolicy(orgId: string) {
  const { auth } = useAuth();
  const done = usePolicyInvalidate();
  return useMutation({
    mutationFn: (policyId: string) => auth!.client.deactivateApprovalPolicy(orgId, policyId),
    onSuccess: done,
  });
}

export function useMyApprovals() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, 'queue'],
    queryFn: () => auth!.client.listMyApprovals(),
    enabled: !!auth,
    staleTime: 15_000,
  });
}

/** Pending approvals I could decide; backs the sidebar badge (polled lightly). */
export function useMyApprovalsCount() {
  const { auth } = useAuth();
  return useQuery<{ count: number }>({
    queryKey: [ROOT, 'count'],
    queryFn: async () => ({ count: (await auth!.client.listMyApprovals()).count }),
    enabled: !!auth,
    refetchInterval: 60_000,
    staleTime: 0,
    retry: false,
  });
}

export function useApprovalRequest(id: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, 'one', id],
    queryFn: () => auth!.client.getApprovalRequest(id!),
    enabled: !!auth && !!id,
    retry: false,
  });
}

export function useDecideApproval(id: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { decision: 'approve' | 'reject'; reason: string }) =>
      auth!.client.decideApprovalRequest(id, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [ROOT] });
      void qc.invalidateQueries({ queryKey: ['loops'] });
      void qc.invalidateQueries({ queryKey: ['loopStory'] });
      void qc.invalidateQueries({ queryKey: ['ticket-notifications'] });
    },
  });
}

/** Org approval log, keyset-paged by `next_before` (owners and admins). */
export function useApprovalLog(orgId: string | undefined, filters: ApprovalLogFilters) {
  const { auth } = useAuth();
  return useInfiniteQuery({
    queryKey: [ROOT, 'log', orgId, filters],
    queryFn: ({ pageParam }) => auth!.client.getApprovalLog(orgId!, filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before,
    enabled: !!auth && !!orgId,
    placeholderData: keepPreviousData,
    staleTime: 15_000,
    retry: false,
  });
}
