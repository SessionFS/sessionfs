import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useMe } from './useMe';

/**
 * Marketplace results are identity-scoped (private listings). Every query key
 * carries the signed-in user id so a different account on the same browser can
 * never be served another user's cached rows; queries stay disabled until the
 * identity is known.
 */
function useIdentity(): string | null {
  const me = useMe();
  return me.data?.user_id ?? null;
}

export const MARKETPLACE_TOOLS = [
  'claude-code',
  'codex',
  'gemini',
  'cursor',
  'copilot',
  'amp',
  'cline',
  'roo-code',
  'kilo-code',
] as const;

export const MARKETPLACE_PAGE_SIZE = 24;

/**
 * Keyset-paginated catalog. Each page carries `next_cursor`; the page appends
 * the next page on "Load more" instead of silently truncating to the newest N.
 */
export function useMarketplacePackages(q: string, tool: string) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useInfiniteQuery({
    queryKey: ['marketplace', identity, q, tool],
    queryFn: ({ pageParam }) =>
      auth!.client.searchMarketplace({
        q: q.trim() || undefined,
        tool: tool || undefined,
        limit: MARKETPLACE_PAGE_SIZE,
        cursor: pageParam || undefined,
      }),
    initialPageParam: '' as string,
    getNextPageParam: (lastPage) => (lastPage.has_more && lastPage.next_cursor ? lastPage.next_cursor : undefined),
    enabled: !!auth && !!identity,
    staleTime: 30_000,
  });
}

export function useMarketplacePreview(versionId: string | undefined, enabled: boolean) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['marketplace-preview', identity, versionId],
    queryFn: () => auth!.client.previewMarketplaceVersion(versionId!),
    enabled: !!auth && !!identity && !!versionId && enabled,
    staleTime: 60_000,
  });
}

export function useSkillInstallRequests(projectId: string | undefined) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['skill-install-requests', identity, projectId],
    queryFn: () => auth!.client.listSkillInstallRequests(projectId!),
    enabled: !!auth && !!identity && !!projectId,
    staleTime: 15_000,
  });
}

export function useCreateSkillInstallRequest() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, ...body }: {
      projectId: string;
      version_id: string;
      content_hash: string;
      target_scope: 'project' | 'user' | 'device';
      target_device_id?: string;
      target_tools: string[];
      requested_capabilities: string[];
    }) => auth!.client.createSkillInstallRequest(projectId, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['skill-install-requests'] });
    },
  });
}

export function useApproveSkillInstallRequest(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      requestId,
      approvedCapabilities,
      confirmationCode,
    }: { requestId: string; approvedCapabilities: string[]; confirmationCode?: string }) =>
      auth!.client.approveSkillInstallRequest(projectId, requestId, approvedCapabilities, confirmationCode),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['skill-install-requests'] }),
  });
}

export function useChallengeSkillInstallRequest(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (requestId: string) => auth!.client.challengeSkillInstallRequest(projectId, requestId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['skill-install-requests'] }),
  });
}

export function useRevokeSkillInstallRequest(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (requestId: string) => auth!.client.revokeSkillInstallRequest(projectId, requestId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['skill-install-requests'] }),
  });
}

export function useCancelSkillInstallRequest(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (requestId: string) => auth!.client.cancelSkillInstallRequest(projectId, requestId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['skill-install-requests'] }),
  });
}

export function useEffectiveSkillAssignments(tool = '') {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['skill-assignments-effective', identity, tool],
    queryFn: () => auth!.client.listEffectiveSkillAssignments({ tool: tool || undefined }),
    enabled: !!auth && !!identity,
    staleTime: 30_000,
  });
}

export function useOrgSkillControl(orgId: string | null | undefined) {
  const { auth } = useAuth();
  const identity = useIdentity();
  const registries = useQuery({
    queryKey: ['skill-registries', identity, orgId],
    queryFn: () => auth!.client.listOrgSkillRegistries(orgId!),
    enabled: !!auth && !!identity && !!orgId,
    staleTime: 30_000,
  });
  const assignments = useQuery({
    queryKey: ['skill-assignments-org', identity, orgId],
    queryFn: () => auth!.client.listOrgSkillAssignments(orgId!),
    enabled: !!auth && !!identity && !!orgId,
    staleTime: 30_000,
  });
  return { registries, assignments };
}

export function useDeletePersonalSkillVersion() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packageId, versionId }: { packageId: string; versionId: string }) =>
      auth!.client.deletePersonalSkillVersion(packageId, versionId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['marketplace'] }),
  });
}
