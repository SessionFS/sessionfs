import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useMe } from './useMe';
import type {
  ApprovalBinding,
  MarketplaceSearchFilters,
  ModerationAction,
  PackageSettingsInput,
  ScanFindingKey,
  ModerationTargetKind,
  PublishPackageInput,
  ReportReason,
  RevocationEvent,
} from '../api/client';
import {
  RevocationFeedIncompleteError,
  buildRevocationIndex,
  isRevocationFeedIncomplete,
  type RevocationIndex,
} from '../marketplace/moderationState';

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
export type MarketplaceFilterState = Omit<MarketplaceSearchFilters, 'q' | 'tool' | 'limit' | 'cursor'>;

export function useMarketplacePackages(q: string, tool: string, filters: MarketplaceFilterState = {}) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useInfiniteQuery({
    queryKey: ['marketplace', identity, q, tool, filters],
    queryFn: ({ pageParam }) =>
      auth!.client.searchMarketplace({
        ...filters,
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

export function useMarketplaceCategories() {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['marketplace-categories', identity],
    queryFn: () => auth!.client.listMarketplaceCategories(),
    enabled: !!auth && !!identity,
    staleTime: 10 * 60_000,
  });
}

export function useMarketplaceReadme(packageId: string | undefined, versionId: string | undefined, enabled: boolean) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['marketplace-readme', identity, packageId, versionId],
    queryFn: () => auth!.client.getMarketplaceReadme(packageId!, versionId),
    enabled: !!auth && !!identity && !!packageId && enabled,
    staleTime: 60_000,
  });
}

export function usePublisherAnalytics(publisherId: string | undefined, days: number) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['publisher-analytics', identity, publisherId, days],
    queryFn: () => auth!.client.getPublisherAnalytics(publisherId!, days),
    enabled: !!auth && !!identity && !!publisherId,
    staleTime: 60_000,
    retry: false,
  });
}

export function useUpdatePackageSettings(publisherId: string) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ packageId, body }: { packageId: string; body: PackageSettingsInput }) =>
      auth!.client.updatePackageSettings(publisherId, packageId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['marketplace'] });
      void queryClient.invalidateQueries({ queryKey: ['publisher-analytics'] });
    },
  });
}

export function useOrgSkillUsage(orgId: string | undefined, days: number) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['org-skill-usage', identity, orgId, days],
    queryFn: () => auth!.client.getOrgSkillUsage(orgId!, days),
    enabled: !!auth && !!identity && !!orgId,
    staleTime: 60_000,
    retry: false,
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
      acknowledgements,
    }: {
      requestId: string;
      approvedCapabilities: string[];
      confirmationCode?: string;
      acknowledgements?: ApprovalBinding;
    }) =>
      acknowledgements
        ? auth!.client.approveSkillInstallRequest(projectId, requestId, approvedCapabilities, confirmationCode, acknowledgements)
        : auth!.client.approveSkillInstallRequest(projectId, requestId, approvedCapabilities, confirmationCode),
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

/** "Poll now" for an org registry: reschedules it for the next poller run. */
export function useRequestRegistryPoll(orgId: string | null | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (registryId: string) => auth!.client.requestOrgSkillRegistryPoll(orgId!, registryId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['skill-registries'] }),
  });
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

// ── Marketplace v2: publishing, reports, moderation ──

export function usePublishSkillPackage() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: PublishPackageInput) => auth!.client.publishSkillPackage(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['marketplace'] }),
  });
}

export function useReportMarketplacePackage() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packageId, ...body }: { packageId: string; reason: ReportReason; details?: string; version_id?: string }) =>
      auth!.client.reportMarketplacePackage(packageId, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['marketplace-reports'] }),
  });
}

export const MARKETPLACE_REPORTS_PAGE_SIZE = 50;

export function useMarketplaceReports(status: string, enabled = true) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useInfiniteQuery({
    queryKey: ['marketplace-reports', identity, status],
    queryFn: ({ pageParam }) =>
      auth!.client.listMarketplaceReports({
        status,
        limit: MARKETPLACE_REPORTS_PAGE_SIZE,
        cursor: pageParam || undefined,
      }),
    initialPageParam: '' as string,
    getNextPageParam: (lastPage) => (lastPage.has_more && lastPage.next_cursor ? lastPage.next_cursor : undefined),
    enabled: !!auth && !!identity && enabled,
    staleTime: 15_000,
  });
}

export function useDismissMarketplaceReport() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ reportId, note }: { reportId: string; note: string }) =>
      auth!.client.dismissMarketplaceReport(reportId, note),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['marketplace-reports'] }),
  });
}

export function useModerateMarketplace() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, targetId, action, reason, findings }: {
      kind: ModerationTargetKind;
      targetId: string;
      action: ModerationAction;
      reason: string;
      findings?: ScanFindingKey[];
    }) =>
      findings
        ? auth!.client.moderateMarketplace(kind, targetId, action, reason, findings)
        : auth!.client.moderateMarketplace(kind, targetId, action, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['marketplace-reports'] });
      void qc.invalidateQueries({ queryKey: ['marketplace-revocations'] });
      void qc.invalidateQueries({ queryKey: ['marketplace'] });
    },
  });
}

/** Listing lookup for the moderation queue; 404 (yanked/blocked/hidden) is an answer, not a retry. */
export function useMarketplacePackageInfo(packageId: string | undefined) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['marketplace-package', identity, packageId],
    queryFn: () => auth!.client.getMarketplacePackage(packageId!),
    enabled: !!auth && !!identity && !!packageId,
    retry: false,
    staleTime: 30_000,
  });
}

/**
 * Safety cap on feed pages per refresh (500 events each = 100,000 events).
 * The feed is read until the server says `has_more: false`; if this cap is hit
 * first, the query fails with RevocationFeedIncompleteError (the previous
 * complete index, if any, stays cached) rather than caching partial state.
 */
export const REVOCATION_FEED_MAX_PAGES = 200;
const REVOCATION_FEED_PAGE = 500;

/**
 * Yank / block / restore state for every package and version visible to the
 * caller, replayed from the revocations feed. Yanked and blocked versions are
 * already absent from search, so this mainly protects stale cached rows and
 * labels versions shown elsewhere (install requests, the moderation queue).
 */
export function useRevocationIndex(enabled = true) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['marketplace-revocations', identity],
    queryFn: async (): Promise<RevocationIndex> => {
      const events: RevocationEvent[] = [];
      let since: string | undefined;
      let complete = false;
      for (let page = 0; page < REVOCATION_FEED_MAX_PAGES; page += 1) {
        const result = await auth!.client.listMarketplaceRevocations({ since, limit: REVOCATION_FEED_PAGE });
        const batch = result?.revocations ?? [];
        events.push(...batch);
        const advanced = !!result?.next_since && result.next_since !== since;
        if (typeof result?.has_more === 'boolean') {
          if (!result.has_more) {
            complete = true;
            break;
          }
          if (!advanced) break; // claims more but does not move: incomplete
        } else if (batch.length === 0 || !advanced) {
          complete = true; // older server: an empty / non-advancing page ends it
          break;
        }
        since = result.next_since;
      }
      // A truncated replay must never be cached or used for gating: fail the
      // query so react-query keeps the previous COMPLETE index (if any) and
      // consumers show the feed as incomplete.
      if (!complete) throw new RevocationFeedIncompleteError();
      return buildRevocationIndex(events);
    },
    // Re-reading a feed that hit the cap would hit it again; other errors retry.
    retry: (failures, error) => !isRevocationFeedIncomplete(error) && failures < 3,
    enabled: !!auth && !!identity && enabled,
    staleTime: 30_000,
  });
}
