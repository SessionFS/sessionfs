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
import { ApiError } from '../api/client';

export interface OrgGeneralSettings {
  kb_retention_days: number | null;
  kb_max_context_words: number | null;
  kb_section_page_limit: number | null;
  helper_retention_days: number | null;
  auto_join_domains?: string[];
  /** GitHub #101 — restricted project visibility. Always a bool on read;
   *  omit it on a PUT to leave it unchanged. */
  restrict_project_visibility?: boolean;
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
  return useMutation<OrgGeneralSettings, Error, Partial<OrgGeneralSettings>>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/settings`, {
        method: 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        // ApiError parses the error envelope, so callers get a readable
        // message and can branch on codes such as upgrade_required.
        throw new ApiError(resp.status, await resp.text());
      }
      return resp.json();
    },
    onSuccess: (_data, body) => {
      void queryClient.invalidateQueries({ queryKey: ['orgSettings', orgId] });
      // Restricting visibility changes which projects members can see and edit.
      if (body.restrict_project_visibility !== undefined) {
        void queryClient.invalidateQueries({ queryKey: ['projects'] });
        void queryClient.invalidateQueries({ queryKey: ['project'] });
      }
    },
  });
}
