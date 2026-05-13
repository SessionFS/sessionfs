/**
 * React-Query hooks for v0.10.0 Phase 6 org-level general settings.
 *
 * Targets GET/PUT /api/v1/orgs/{org_id}/settings. DLP policy is a
 * separate route (/api/v1/dlp/policy); the two surfaces share the
 * underlying Organization.settings JSON column but expose different
 * keys and feature gates.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

export interface OrgGeneralSettings {
  kb_retention_days: number | null;
  kb_max_context_words: number | null;
  kb_section_page_limit: number | null;
}

function useApiBase(): { base: string; headers: { Authorization: string } } {
  const { auth } = useAuth();
  const base =
    auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  return { base, headers };
}

export function useOrgSettings(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  return useQuery<OrgGeneralSettings>({
    queryKey: ['orgSettings', orgId],
    queryFn: async () => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/settings`, { headers });
      if (!resp.ok) throw new Error(`Failed to load settings: ${resp.status}`);
      return resp.json();
    },
    enabled: !!orgId,
    staleTime: 60_000,
  });
}

export function useUpdateOrgSettings(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation<OrgGeneralSettings, Error, OrgGeneralSettings>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/settings`, {
        method: 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Settings update failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['orgSettings', orgId] });
    },
  });
}
