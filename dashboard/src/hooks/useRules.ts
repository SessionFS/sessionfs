import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { ApiError, type ProjectRules } from '../api/client';

/**
 * React-Query hooks for the v0.9.9 Project Rules system.
 *
 * The shape matches the brief's §API section:
 *   GET  /api/v1/projects/{id}/rules                    -> canonical rules + ETag
 *   PUT  /api/v1/projects/{id}/rules                    -> update (If-Match ETag)
 *   POST /api/v1/projects/{id}/rules/compile            -> trigger compile
 *   GET  /api/v1/projects/{id}/rules/versions           -> list of versions
 *   GET  /api/v1/projects/{id}/rules/versions/{version} -> one version + compiled_outputs
 */

export function useProjectRules(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['projectRules', projectId],
    queryFn: () => auth!.client.getProjectRules(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 30_000,
  });
}

export function useUpdateProjectRules(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ rules, etag }: { rules: Partial<ProjectRules>; etag: string }) =>
      auth!.client.updateProjectRules(projectId!, rules, etag),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projectRules', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['rulesVersions', projectId] });
    },
  });
}

export function useCompileRules(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.compileRules(projectId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projectRules', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['rulesVersions', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['rulesVersion', projectId] });
    },
  });
}

export function useRulesVersions(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['rulesVersions', projectId],
    queryFn: () => auth!.client.listRulesVersions(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 30_000,
  });
}

export function useRulesVersion(projectId: string | undefined, version: number | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['rulesVersion', projectId, version],
    queryFn: () => auth!.client.getRulesVersion(projectId!, version!),
    enabled: !!auth && !!projectId && version !== null,
    staleTime: 60_000,
  });
}

/**
 * Narrow helper so UI code can detect the 409-stale-ETag case without
 * importing ApiError everywhere.
 */
export function isStaleEtagError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409;
}
