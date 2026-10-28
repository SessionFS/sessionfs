/**
 * React-Query hooks for the org-wide LLM key (migration 084).
 *
 * Targets GET/PUT/DELETE /api/v1/orgs/{org_id}/llm-settings. The GET
 * response never carries the key itself — it reports `has_api_key` and a
 * last-4 `key_hint`, so a PUT that omits `apiKey` keeps the stored key.
 */

import { useCallback } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type { LlmConnectionTestBody, OrgLlmSettings } from '../api/client';

export function useOrgLlmSettings(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<OrgLlmSettings>({
    queryKey: ['orgLlmSettings', orgId],
    queryFn: () => auth!.client.getOrgLlmSettings(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

export function useSaveOrgLlmSettings(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (body: {
      provider: string;
      model: string;
      apiKey?: string;
      baseUrl?: string | null;
      enabled: boolean;
    }) => auth!.client.saveOrgLlmSettings(orgId!, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['orgLlmSettings', orgId] });
    },
  });
}

export function useDeleteOrgLlmSettings(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => auth!.client.deleteOrgLlmSettings(orgId!),
    onSuccess: () => {
      queryClient.setQueryData<OrgLlmSettings>(['orgLlmSettings', orgId], (prev) => ({
        provider: '',
        model: '',
        has_api_key: false,
        key_hint: null,
        base_url: null,
        enabled: true,
        plan_includes_helper: prev?.plan_includes_helper ?? true,
      }));
    },
  });
}

/** "Test connection" for the org helper key form (POST .../llm-settings/test). */
export function useOrgLlmConnectionTester(orgId: string | undefined) {
  const { auth } = useAuth();
  return useCallback(
    (body: LlmConnectionTestBody) => auth!.client.testOrgLlmConnection(orgId!, body),
    [auth, orgId],
  );
}

/** "Fetch models" for the org helper key form (POST .../llm-settings/models). */
export function useOrgModelDiscovery(orgId: string | undefined) {
  const { auth } = useAuth();
  return useCallback(
    (baseUrl: string, apiKey: string | undefined, provider: string | undefined) =>
      auth!.client.discoverOrgModels(orgId!, baseUrl, apiKey, provider),
    [auth, orgId],
  );
}
